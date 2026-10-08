#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 Juliana Tomazini
# Licensed under the EUPL
"""
Earshot - local name matching against bulk-downloaded news feeds.

How it keeps names private
--------------------------
The script downloads every feed in feeds.txt and every new article those
feeds link to, whether or not anything matches. The matching against
names.txt happens afterwards, on this computer. The functions that talk to
the network only ever receive URLs taken from feeds.txt or from the feeds
themselves; names are never placed in a URL, header, or request body.
No search engine or API is queried.

One optional step breaks that rule, and only if you switch it on in
settings.txt: a language model can be asked whether a match is the listed
person or a namesake. The person's name and the text around the match are then
sent to the model address you set. That address must be on this computer
unless you explicitly allow another one.

Files (all in this folder)
--------------------------
names.txt     your list - one person per line, variants after "|"
feeds.txt     the outlets to watch - "Label | feed URL"
report.html   written on every run - open it in a browser
matches.csv   full match history, opens in Excel
diagnostics.txt   written by --diagnose only
settings.txt  optional: switches on the language-model step (see settings.example.txt)
data/         local database of what has already been checked

Usage
-----
python3 earshot.py                 normal run
python3 earshot.py --check-feeds   test every feed, no matching
python3 earshot.py --diagnose      write diagnostics.txt: what each feed and a
                                         couple of its articles return (names.txt is not read)
python3 earshot.py --audit         re-read every recent article of every feed and write
                                         coverage_audit.txt and .csv: how much of each outlet is
                                         readable (names.txt and the database are not touched)
python3 earshot.py --headlines-only   skip article pages (faster, misses quotes)
python3 earshot.py --days 60       show the last 60 days in the report
python3 earshot.py --keep-days 180 delete stored matches older than 180 days
python3 earshot.py --check-model   ask the model in settings.txt an invented question
python3 earshot.py --judge-stored  ask the model about stored matches it has not judged yet

Tests:   python3 -m unittest discover -s tests -v     (none of them touches the internet)

On Windows, type "py" or "python" where these lines say "python3", or double-click
"Run Earshot.bat". On a Mac, double-click "Run Earshot.command".

Needs Python 3.9 or later. Uses the standard library only, plus the operating
system's own curl program (Windows 10/11 and macOS include it) as a fallback
when a site refuses the first request.
"""

import argparse
import csv
import gzip
import html
import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

__version__ = "1.1.4"

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES_FILE = os.path.join(HERE, "names.txt")
FEEDS_FILE = os.path.join(HERE, "feeds.txt")
DATA_DIR = os.path.join(HERE, "data")
DB_FILE = os.path.join(DATA_DIR, "monitor.db")
REPORT_FILE = os.path.join(HERE, "report.html")
DIAG_FILE = os.path.join(HERE, "diagnostics.txt")
AUDIT_TXT = os.path.join(HERE, "coverage_audit.txt")
AUDIT_CSV = os.path.join(HERE, "coverage_audit.csv")
SETTINGS_FILE = os.path.join(HERE, "settings.txt")
CSV_FILE = os.path.join(HERE, "matches.csv")

ROBOT_NAME = "earshot"
USER_AGENT = "Mozilla/5.0 (compatible; earshot/1.0; personal feed reader)"
PLAIN_AGENT = "earshot/1.0 (personal feed reader)"
ACCEPT = "text/html,application/xhtml+xml,application/xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.8"
ACCEPT_LANGUAGE = "en,it;q=0.8,fr;q=0.6,de;q=0.6,es;q=0.6"
TIMEOUT = 25            # seconds per request
MODEL_TIMEOUT = 300     # seconds to wait for one answer from the language model, if one is switched on
MAX_BYTES = 4_000_000   # stop reading a page after this many bytes
HOST_DELAY = 1.0        # seconds between two requests to the same site
PAUSE = 6.0             # wait this long before one more try when a site that was answering refuses a page
WORKERS = 12            # sites read in parallel (one request at a time per site)
MAX_PER_FEED = 80       # new articles taken from one feed in one run
THIN_BODY = 1200        # fewer characters than this = a short item (or an unparsed page)
PAYWALL_TEASER = 2000   # a declared paywall with less text than this = only a teaser was shown
MAX_ATTEMPTS = 3        # an unreachable article is tried again on later runs, up to this many times
# schema.org flag that publishers put in a page to declare it is behind a paywall
PAYWALL_FLAG = re.compile(r'"isAccessibleForFree"\s*:\s*"?\s*false', re.I)

HIT_START, HIT_END = "\x01", "\x02"   # internal markers around the matched name

NAMES_TEMPLATE = """# NAMES TO WATCH
# This file stays on this computer. The script never sends its contents anywhere.
#
# One person per line. Put other spellings after a "|":
#     Full Name | Other Spelling | Name Without Middle Initial
#
# - Accents and capitals do not matter: "M\u00fcller" also finds "Muller" and "M\u00dcLLER".
# - Transliterations do matter: add "Djordje" next to "\u0110or\u0111e", "Mueller" next to "M\u00fcller".
# - Use full names. A surname on its own will match every other person with that surname.
# - A line in [square brackets] starts a group; the report can be filtered by group.
# - Optional, only used by the language-model step: after "::" say who the person is,
#       Full Name | Other Spelling :: political scientist at Example University, works on trade
# - Lines starting with # are ignored.

[Group one]

"""


# --------------------------------------------------------------------------
# Text normalisation and matching (local only)
# --------------------------------------------------------------------------

_COMBINING = {i: None for i in range(0x300, 0x370)}
_SPECIAL = str.maketrans({
    "ø": "o", "đ": "d", "ł": "l", "æ": "ae", "œ": "oe", "ı": "i", "ð": "d", "þ": "th",
    "’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "­": None, "​": None,
})


def norm(text):
    """Lower-case, strip accents, unify apostrophes and dashes."""
    text = unicodedata.normalize("NFKD", text).translate(_COMBINING)
    return text.casefold().translate(_SPECIAL)


def squash(text):
    return " ".join(text.split())


class Matcher:
    def __init__(self, people):
        # people: list of (canonical name, group, [variants]) or (..., note about the person)
        self.lookup = {}
        self.notes = {}
        variants = []
        for person in people:
            name, group, forms = person[:3]
            self.notes[name] = person[3] if len(person) > 3 else ""
            for form in forms:
                key = squash(norm(form))
                if key and key not in self.lookup:
                    self.lookup[key] = (name, group)
                    variants.append(key)
        variants.sort(key=len, reverse=True)
        self.regex = None
        if variants:
            body = "|".join(re.escape(v) for v in variants)
            self.regex = re.compile(r"(?<!\w)(?:" + body + r")(?!\w)")

    def find(self, text):
        """Return {canonical name: (group, snippet, wider excerpt)} for one piece of text."""
        if not self.regex or not text:
            return {}
        text = squash(text)
        if not self.regex.search(norm(text)):      # fast path: nothing here
            return {}
        # Slow path, only for texts with a match: keep a map back to the
        # original characters so the snippet keeps its accents and capitals.
        chars, origin = [], []
        for i, ch in enumerate(text):
            for n in norm(ch):
                chars.append(n)
                origin.append(i)
        normalised = "".join(chars)
        found = {}
        for m in self.regex.finditer(normalised):
            name, group = self.lookup.get(m.group(0), (None, None))
            if not name or name in found:
                continue
            start, end = origin[m.start()], origin[m.end() - 1] + 1
            left = max(0, start - 160)
            right = min(len(text), end + 160)
            snippet = (("... " if left else "") + text[left:start] + HIT_START
                       + text[start:end] + HIT_END + text[end:right]
                       + (" ..." if right < len(text) else ""))
            wide = text[max(0, start - 700):min(len(text), end + 700)]
            found[name] = (group, snippet, wide)
        return found


# --------------------------------------------------------------------------
# Reading names.txt and feeds.txt
# --------------------------------------------------------------------------

def read_text_file(path):
    """Read a text file whatever the editor saved it as (UTF-8, UTF-16 or Windows ANSI)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def read_lines(path):
    for raw in read_text_file(path).splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def load_names():
    people, group, warnings = [], "", []
    for line in read_lines(NAMES_FILE):
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip()
            continue
        line, _, note = line.partition("::")         # optional: who this person is, for the model step
        forms = [squash(p) for p in line.split("|") if p.strip()]
        if not forms:
            continue
        for form in forms:
            if " " not in form and len(form) < 6:
                warnings.append('"%s" is short and will match unrelated text' % form)
        people.append((forms[0], group, forms, squash(note)))
    return people, warnings


def load_settings():
    settings = {"model_address": "", "model_name": "", "model_key": "", "allow_remote_model": "no",
                "context": "", "thinking": "on"}
    if os.path.exists(SETTINGS_FILE):
        for line in read_lines(SETTINGS_FILE):
            key, sep, value = line.partition("=")
            key = key.strip().lower()
            if sep and key in settings:
                settings[key] = value.strip()
            else:
                print("  ! settings.txt: line not understood: %s" % line)
    return settings


def load_feeds():
    feeds, section = [], ""
    for line in read_lines(FEEDS_FILE):
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 1:
            label, url, opts = urllib.parse.urlsplit(parts[0]).netloc, parts[0], ""
        else:
            label, url, opts = parts[0], parts[1], " ".join(parts[2:]).lower()
        if not url.lower().startswith(("http://", "https://")):
            print("  ! skipped line in feeds.txt (no http address): %s" % line)
            continue
        feeds.append({"label": label, "url": url, "section": section,
                      "headlines_only": "headlines-only" in opts})
    return feeds


# --------------------------------------------------------------------------
# Network (receives URLs only - never names)
# --------------------------------------------------------------------------

def make_ssl_context():
    ctx = ssl.create_default_context()
    try:
        # Python installed from python.org on macOS can start with an empty
        # certificate store; fall back to the system bundle. Verification
        # always stays on.
        if ctx.cert_store_stats().get("x509_ca", 0) == 0 and os.path.exists("/etc/ssl/cert.pem"):
            ctx.load_verify_locations(cafile="/etc/ssl/cert.pem")
    except Exception:
        pass
    return ctx


SSL_CONTEXT = make_ssl_context()
CURL = shutil.which("curl")
_host_locks = {}
_host_last = {}
_host_state = {}     # site -> {"method": index of the way that worked, "refusals": count}
_registry_lock = threading.Lock()


def _get_urllib(url, agent):
    req = urllib.request.Request(url, headers={
        "User-Agent": agent,
        "Accept": ACCEPT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept-Encoding": "gzip, deflate",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CONTEXT) as resp:
            data = resp.read(MAX_BYTES)
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            charset = resp.headers.get_content_charset()
    except urllib.error.HTTPError as err:
        err.close()                 # release the connection; the status code stays readable
        raise
    if encoding == "gzip" or data[:2] == b"\x1f\x8b":
        try:
            data = zlib.decompressobj(31).decompress(data)   # tolerates a cut-off stream
        except zlib.error:
            data = gzip.decompress(data)
    elif encoding == "deflate":
        try:
            data = zlib.decompress(data)
        except zlib.error:
            data = zlib.decompress(data, -zlib.MAX_WBITS)
    return data, charset


def _get_curl(url, agent):
    """Same request through the system's curl, for sites that turn Python away."""
    command = [CURL, "--silent", "--show-error", "--location",
               "--max-redirs", "5", "--max-time", str(TIMEOUT), "--max-filesize", str(MAX_BYTES),
               "--proto", "=http,https", "--proto-redir", "=http,https",
               "--user-agent", agent, "--header", "Accept: " + ACCEPT,
               "--header", "Accept-Language: " + ACCEPT_LANGUAGE,
               "--write-out", "\n%{http_code} %{content_type}"]
    try:
        # curl does not read the system's proxy settings by itself; hand them over
        parts = urllib.parse.urlsplit(url)
        proxy = urllib.request.getproxies().get(parts.scheme)
        if proxy and not urllib.request.proxy_bypass(parts.hostname or ""):
            command += ["--proxy", proxy]
    except Exception:
        pass
    result = subprocess.run(command + ["--", url], capture_output=True, timeout=TIMEOUT + 10)
    if result.returncode != 0:
        raise urllib.error.URLError("curl error %d" % result.returncode)
    body, _, tail = result.stdout.rpartition(b"\n")
    if body[:2] == b"\x1f\x8b":            # sent compressed although not asked for
        try:
            body = zlib.decompressobj(31).decompress(body)
        except zlib.error:
            pass
    code, _, content_type = tail.decode("ascii", "replace").partition(" ")
    if not code.isdigit():
        raise urllib.error.URLError("curl gave no status")
    if int(code) >= 400:
        raise urllib.error.HTTPError(url, int(code), "refused", None, None)
    m = re.search(r"charset=[\"']?([\w-]+)", content_type)
    return body, (m.group(1) if m else None)


# Ways of asking, tried in this order when a site answers 403 (refused).
# Every one of them says plainly that it is an automated reader.
METHODS = [
    ("standard", lambda url: _get_urllib(url, USER_AGENT)),
    ("plain identity", lambda url: _get_urllib(url, PLAIN_AGENT)),
]
if CURL:
    METHODS.append(("curl", lambda url: _get_curl(url, PLAIN_AGENT)))


def fetch(url):
    """Download one URL politely. Returns (bytes, charset or None)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("not a web address")
    host = parts.netloc.lower()
    with _registry_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
        state = _host_state.setdefault(host, {"method": None, "refusals": 0})
    with lock:
        known = state["method"] or 0
        if state["refusals"] >= 2:
            order = [known]                      # it keeps refusing: stop knocking
        else:
            order = range(known, len(METHODS))   # start from what last worked here
        refused = None
        for index in order:
            wait = _host_last.get(host, 0) + HOST_DELAY - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                result = METHODS[index][1](url)
            except urllib.error.HTTPError as err:
                if err.code != 403:
                    raise
                refused = err
                continue
            finally:
                _host_last[host] = time.time()
            state["method"] = index
            return result
        # A site that answered before and now says 403 may only be asking us to slow down:
        # wait and ask once more. Given up for the site after four such waits in a row fail.
        if state["method"] is not None and state.get("pause_streak", 0) < 4:
            time.sleep(PAUSE)
            try:
                result = METHODS[state["method"]][1](url)
            except urllib.error.HTTPError as err:
                if err.code != 403:
                    raise
                state["pause_streak"] = state.get("pause_streak", 0) + 1
                state["pause_failed"] = state.get("pause_failed", 0) + 1
            else:
                state["pause_streak"] = 0
                state["pause_ok"] = state.get("pause_ok", 0) + 1
                return result
            finally:
                _host_last[host] = time.time()
        state["refusals"] += 1
        raise refused


def method_note(url):
    """Says which way of asking a site accepted, when it was not the first."""
    state = _host_state.get(urllib.parse.urlsplit(url).netloc.lower()) or {}
    index = state.get("method")
    return " (accepted via %s)" % METHODS[index][0] if index else ""


def site_rules(origin):
    """Read a site's robots.txt once. Article pages it rules out are not opened."""
    # Follows RFC 9309: a robots.txt that answers with a 4xx code sets no rules;
    # one that cannot be reached at all (5xx, network error) means "do not read".
    rules = urllib.robotparser.RobotFileParser()
    rules.why = "ruled out by robots.txt"
    rules.unreachable = ""
    try:
        data, _ = fetch(origin + "/robots.txt")
        rules.parse(data.decode("utf-8", "replace").splitlines())
    except urllib.error.HTTPError as err:
        if 400 <= err.code < 500:
            rules.allow_all = True
        else:
            rules.unreachable = "robots.txt could not be read (HTTP %d), so the page was not opened" % err.code
    except Exception as err:
        rules.unreachable = "robots.txt could not be read (%s), so the page was not opened" % explain(err)
    return rules


def origin_of(url):
    parts = urllib.parse.urlsplit(url)
    return "%s://%s" % (parts.scheme, parts.netloc)


def explain(err):
    if isinstance(err, urllib.error.HTTPError):
        if err.code == 403:
            return "HTTP 403 - the site refuses automated readers (%d ways tried)" % len(METHODS)
        return "HTTP %s" % err.code
    if isinstance(err, urllib.error.URLError):
        return "connection failed (%s)" % err.reason
    if isinstance(err, ET.ParseError):
        return "not a readable feed"
    return "%s: %s" % (type(err).__name__, err)


def decode(data, charset):
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', data[:3000])
        charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Feed and article parsing
# --------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _text(node):
    return squash("".join(node.itertext())) if node is not None else ""


def strip_tags(fragment):
    return squash(html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")))


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_url(url):
    parts = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(query), ""))


def parse_feed(data, base_url):
    root = ET.fromstring(data.lstrip())
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = link = summary = author = date = ""
        for child in node:
            tag = _local(child.tag)
            if tag == "title":
                title = strip_tags(_text(child))
            elif tag == "link":
                href = child.get("href")
                if href:
                    if child.get("rel", "alternate") == "alternate" or not link:
                        link = href
                elif not link:
                    link = _text(child)
            elif tag in ("description", "summary", "encoded", "content"):
                text = strip_tags(_text(child))
                if len(text) > len(summary):
                    summary = text
            elif tag in ("creator", "author"):
                author = (author + ", " if author else "") + _text(child)
            elif tag in ("pubdate", "published", "updated", "date") and not date:
                date = _text(child)
        if not link:
            continue
        link = urllib.parse.urljoin(base_url, link)
        if not link.lower().startswith(("http://", "https://")):
            continue
        items.append({"url": clean_url(link), "title": title, "summary": summary,
                      "author": author, "published": parse_date(date)})
    return items


class _PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form",
            "template", "button", "select", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th",
             "tr", "section", "article", "blockquote", "figcaption", "header"}

    def __init__(self, skip):
        super().__init__(convert_charrefs=True)
        self.skip_tags = skip
        self.skipping = 0
        self.in_article = 0
        self.everything = []
        self.article = []

    def _add(self, text):
        self.everything.append(text)
        if self.in_article:
            self.article.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self.skipping += 1
        elif tag == "article":
            self.in_article += 1
        if tag in self.BLOCK:
            self._add(" ")

    def handle_endtag(self, tag):
        if tag in self.skip_tags:
            self.skipping = max(0, self.skipping - 1)
        elif tag == "article":
            self.in_article = max(0, self.in_article - 1)
        if tag in self.BLOCK:
            self._add(" ")

    def handle_data(self, data):
        if not self.skipping:
            self._add(data)


LOOSE_SKIP = {"script", "style", "noscript", "svg", "template"}


def page_parts(markup, skip):
    """(text inside <article> tags, all text) with the given tags left out."""
    parser = _PageText(skip)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    return squash("".join(parser.article)), squash("".join(parser.everything))


def page_text_info(markup):
    """(readable text of an article page, where it was taken from)."""
    article, everything = page_parts(markup, _PageText.SKIP)
    if len(everything) < 500 and len(markup) > 20000:
        # an unclosed menu or form tag swallowed the page - retry more loosely
        article, everything = page_parts(markup, LOOSE_SKIP)
    # Trust the <article> tag only when it holds a good share of the page: some
    # sites put just the opening paragraph in it and the rest of the story outside.
    if len(article) >= 600 and len(article) >= 0.4 * len(everything):
        return article, "article tag"
    return everything, "whole page"


def page_text(markup):
    return page_text_info(markup)[0]


BOT_CHECK_SIGNS = ("client challenge", "enable javascript", "just a moment", "cf-chl", "captcha",
                   "are you a robot", "are you human", "access denied", "datadome")


def is_bot_check(markup, text):
    """A near-empty page that asks the visitor to prove it is a browser."""
    if len(text) >= 600:
        return False
    low = markup.lower()
    return any(sign in low for sign in BOT_CHECK_SIGNS)


def classify(markup, text, feed_text):
    """How much of an article was readable: ok, paywalled (teaser only) or thin (short)."""
    longest = max(len(text), len(feed_text or ""))
    if PAYWALL_FLAG.search(markup) and longest < PAYWALL_TEASER:
        return "paywalled"
    return "ok" if longest >= THIN_BODY else "thin"


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Optional step: ask a language model whether a match is the listed person
# --------------------------------------------------------------------------

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
VERDICTS = {"same": "likely the listed person", "other": "likely a namesake",
            "unclear": "unclear", "not judged": "not judged"}
JUDGE_SYSTEM = (
    "You check press mentions for a university communications office. You are given a person the office "
    "follows and an excerpt of a news article in which that name appears. Decide whether the article is about "
    "that specific person or about someone else who has the same name. Use only what the excerpt says. "
    "Answer with a JSON object and nothing else: {\"verdict\": \"same_person\" or \"different_person\" or "
    "\"unclear\", \"reason\": \"one sentence quoting the words in the excerpt that decided it\"}. "
    "Answer \"unclear\" when the excerpt gives no way of telling.")


def parse_verdict(content):
    """Turn a model's answer into (verdict, reason). Never raises."""
    content = re.sub(r"<think>.*?</think>", " ", content or "", flags=re.S)
    found = re.search(r"\{.*\}", content, re.S)
    try:
        answer = json.loads(found.group(0))
        said = str(answer.get("verdict", "")).lower()
        reason = squash(str(answer.get("reason", "")))[:300]
    except Exception:
        return "unclear", "the model's answer could not be read"
    if "different" in said or "other" in said or "namesake" in said:
        return "other", reason
    if "same" in said:
        return "same", reason
    return "unclear", reason


class Judge:
    """Talks to one OpenAI-compatible model endpoint. Refuses addresses that are not on this computer."""

    def __init__(self, settings):
        address = settings["model_address"].rstrip("/")
        parts = urllib.parse.urlsplit(address)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("settings.txt: model_address is not a web address: %s" % address)
        if not settings["model_name"]:
            raise ValueError("settings.txt: model_name is missing")
        self.local = parts.hostname.lower() in LOCAL_HOSTS
        allowed = settings["allow_remote_model"].lower() in ("yes", "true", "1")
        # Ollama on this computer forwards "cloud" models to Ollama's own servers, so a
        # local address alone does not mean the text stays here.
        if re.search(r"(^|[:\-])cloud($|[:\-])", settings["model_name"].lower()):
            self.local = False
            if not allowed:
                raise ValueError(
                    "settings.txt: model_name (%s) is a cloud model: Ollama would forward the names and the\n"
                    "text around each match to its own servers, so nothing was sent. Choose a model that\n"
                    "runs on this computer (for example qwen3:14b), or, if you are allowed to send them\n"
                    "there, add:  allow_remote_model = yes" % settings["model_name"])
        if not self.local and not allowed:
            raise ValueError(
                "settings.txt: model_address (%s) is not on this computer.\n"
                "Names and the text around each match would be sent there, so nothing was sent.\n"
                "If that server is one you are allowed to send them to, add:  allow_remote_model = yes"
                % parts.netloc)
        self.url = address + "/chat/completions"
        self.model = settings["model_name"]
        self.key = settings["model_key"]
        self.context = settings["context"]
        # "thinking = off" asks Qwen3-type models to answer without step-by-step reasoning:
        # much faster, accuracy not measured. Other models ignore the switch.
        self.no_think = settings.get("thinking", "on").lower() in ("off", "no", "false", "0")
        self.label = "%s at %s" % (self.model, "this computer" if self.local else parts.netloc)
        self.failures = 0
        handlers = [urllib.request.HTTPSHandler(context=SSL_CONTEXT)]
        if self.local:
            handlers.append(urllib.request.ProxyHandler({}))     # a local model is never reached through a proxy
        self.opener = urllib.request.build_opener(*handlers)

    def ask(self, messages, json_mode=True):
        body = {"model": self.model, "messages": messages, "temperature": 0, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "User-Agent": PLAIN_AGENT}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), headers=headers)
        try:
            with self.opener.open(req, timeout=MODEL_TIMEOUT) as resp:
                answer = json.loads(resp.read(MAX_BYTES).decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            err.close()
            if err.code == 400 and json_mode:        # this server does not know JSON mode: ask plainly
                return self.ask(messages, json_mode=False)
            raise
        return answer["choices"][0]["message"].get("content") or ""

    def judge(self, person, note, outlet, headline, excerpt):
        """Returns (verdict, reason). A model that cannot be reached never blocks or drops a match."""
        if self.failures >= 2:
            return "not judged", "skipped: the model could not be reached on the two previous attempts"
        about = "; ".join(x for x in (note, self.context) if x) or "no description available"
        question = ("Person we follow: %s\nWhat we know about them: %s\n\nOutlet: %s\nHeadline: %s\n"
                    "Excerpt: %s" % (person, about, outlet, headline, excerpt))
        if self.no_think:
            question += "\n/no_think"
        try:
            content = self.ask([{"role": "system", "content": JUDGE_SYSTEM},
                                {"role": "user", "content": question}])
        except (TimeoutError, socket.timeout):
            # the model is there but slow: not a reason to stop asking about the other matches
            return "not judged", "the model did not answer within %d seconds" % MODEL_TIMEOUT
        except Exception as err:
            if isinstance(getattr(err, "reason", None), (TimeoutError, socket.timeout)):
                return "not judged", "the model did not answer within %d seconds" % MODEL_TIMEOUT
            self.failures += 1
            return "not judged", "the model could not be reached (%s)" % explain(err)
        self.failures = 0
        return parse_verdict(content)


def make_judge(settings, required=False):
    """The model step is off unless settings.txt names a model address."""
    if not settings["model_address"]:
        if required:
            sys.exit("No model is set. Copy settings.example.txt to settings.txt and fill in model_address.")
        return None
    try:
        return Judge(settings)
    except ValueError as err:
        sys.exit(str(err))


def check_model(settings):
    judge = make_judge(settings, required=True)
    print("Asking %s an invented question (no real name is sent) ..." % judge.label)
    began = time.time()
    verdict, reason = judge.judge(
        "Maria Example", "professor of economics at Example University, works on trade policy",
        "Mock Daily", "Local bakery wins regional award",
        "Maria Example, who has run the family bakery in Smalltown for thirty years, said the award was a surprise.")
    print("Answer after %.0f seconds: %s - %s" % (time.time() - began, VERDICTS[verdict], reason))
    print("A working model should answer: likely a namesake.")


def forget_unlisted(db, people):
    """Delete stored matches for people no longer in names.txt. Returns how many were deleted."""
    listed = [p[0] for p in people]
    if not listed:
        return 0
    marks = ",".join("?" * len(listed))
    cur = db.execute("DELETE FROM matches WHERE person NOT IN (%s)" % marks, listed)
    db.commit()
    return cur.rowcount


def judge_matches(db, judge, pending, notes):
    """pending: list of (url, person, outlet, headline, excerpt). Stores a verdict for each."""
    counts = {}
    for n, (url, person, outlet, headline, excerpt) in enumerate(pending, 1):
        began = time.time()
        verdict, reason = judge.judge(person, notes.get(person, ""), outlet, headline, excerpt)
        counts[verdict] = counts.get(verdict, 0) + 1
        db.execute("UPDATE matches SET verdict = ?, reason = ?, judge = ? WHERE url = ? AND person = ?",
                   (verdict, reason, judge.label, url, person))
        db.commit()                  # saved one by one: stopping with Ctrl+C keeps what is done
        # no names on screen, so this output can be shared safely
        print("  match %d of %d: %s (%.0f s)" % (n, len(pending), VERDICTS[verdict], time.time() - began))
    return counts


def judge_stored(settings):
    judge = make_judge(settings, required=True)
    people, _ = load_names() if os.path.exists(NAMES_FILE) else ([], [])
    notes = {p[0]: p[3] for p in people}
    db = open_db()
    gone = forget_unlisted(db, people)
    if gone:
        print("Deleted %d stored match%s for people no longer in names.txt." % (gone, "" if gone == 1 else "es"))
    rows = db.execute("SELECT url, person, feed, title, snippet FROM matches WHERE verdict IS NULL "
                      "OR verdict = 'not judged'").fetchall()
    print("Asking %s about %d stored matches ..." % (judge.label, len(rows)))
    counts = judge_matches(db, judge, [(u, p, f, t, plain(s)) for u, p, f, t, s in rows], notes)
    stamp = (db.execute("SELECT MAX(run_at) FROM runs").fetchone()[0]
             or db.execute("SELECT MAX(run_at) FROM coverage").fetchone()[0]     # runs made before 1.0.0
             or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    write_csv(db)
    write_report(db, stamp, 30, len(people))
    db.close()
    print("Report rewritten: " + REPORT_FILE)
    print("Done: " + ", ".join("%s: %d" % (VERDICTS[k], v) for k, v in sorted(counts.items())) if counts else "Nothing to judge.")


def read_article(item, rules, full_text=True):
    """Open one article page. Returns a dict: state, text, detail, chars, source."""
    def result(state, detail, text="", source=""):
        return {"state": state, "text": text, "detail": detail, "chars": len(text), "source": source}

    if not full_text or item["feed"]["headlines_only"]:
        return result("skipped", "headlines-only")
    site = rules.get(origin_of(item["url"]))
    if site is not None and site.unreachable:
        return result("failed", site.unreachable)        # tried again on a later run
    if site is not None and not site.can_fetch(ROBOT_NAME, item["url"]):
        return result("blocked", site.why)
    try:
        data, charset = fetch(item["url"])
    except Exception as err:
        return result("failed", explain(err))
    markup = decode(data, charset)
    text, source = page_text_info(markup)
    if is_bot_check(markup, text):
        return result("challenge", "the site answered with a bot check page instead of the article")
    return result(classify(markup, text, item["summary"]),
                  "%d characters on the page (%s), %d in the feed" % (len(text), source, len(item["summary"])),
                  text, source)


def read_all(queue, full_text=True):
    """Read every queued article, one request at a time per site, sites in parallel."""
    rules = {}
    if full_text:
        origins = sorted({origin_of(i["url"]) for i in queue if not i["feed"]["headlines_only"]})
        with ThreadPoolExecutor(WORKERS) as pool:
            rules = dict(zip(origins, pool.map(site_rules, origins)))
    by_site = {}
    for position, item in enumerate(queue):
        by_site.setdefault(origin_of(item["url"]), []).append(position)
    results = [None] * len(queue)
    done = [0]

    def read_site(positions):
        for position in positions:
            results[position] = read_article(queue[position], rules, full_text)
            done[0] += 1
            if done[0] % 50 == 0:
                print("  %d / %d" % (done[0], len(queue)))

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(read_site, by_site.values()))
    return results


def open_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            url TEXT PRIMARY KEY, feed TEXT, title TEXT, published TEXT,
            first_seen TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS matches (
            url TEXT, person TEXT, grp TEXT, places TEXT, snippet TEXT,
            feed TEXT, section TEXT, title TEXT, published TEXT, found_at TEXT,
            PRIMARY KEY (url, person));
        CREATE TABLE IF NOT EXISTS coverage (
            run_at TEXT, feed TEXT, section TEXT, status TEXT, new_items INTEGER,
            read_ok INTEGER, thin INTEGER, failed INTEGER, blocked INTEGER);
    """)
    # columns added after the first version; older databases are upgraded in place
    db.execute("""CREATE TABLE IF NOT EXISTS runs (
        run_at TEXT PRIMARY KEY, version TEXT, people INTEGER, name_forms INTEGER,
        feeds_total INTEGER, feeds_ok INTEGER, items_in_feeds INTEGER, skipped_old INTEGER,
        skipped_seen INTEGER, skipped_cap INTEGER, queued_new INTEGER, queued_retry INTEGER,
        texts_checked INTEGER, new_matches INTEGER, seconds REAL)""")
    for table, column, spec in (("articles", "detail", "TEXT"),
                                ("articles", "attempts", "INTEGER DEFAULT 1"),
                                ("articles", "checked_at", "TEXT"),
                                ("coverage", "paywalled", "INTEGER DEFAULT 0"),
                                ("coverage", "items", "INTEGER"),
                                ("coverage", "how", "TEXT"),
                                ("matches", "verdict", "TEXT"),
                                ("matches", "reason", "TEXT"),
                                ("matches", "judge", "TEXT"),
                                ("runs", "judged", "INTEGER"),
                                ("runs", "judge", "TEXT")):
        if column not in [row[1] for row in db.execute("PRAGMA table_info(%s)" % table)]:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, spec))
    return db


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------

def check_feeds(feeds):
    print("Testing %d feeds ...\n" % len(feeds))

    def test(feed):
        try:
            data, _ = fetch(feed["url"])
            items = parse_feed(data, feed["url"])
            if not items:
                return "FAIL  no items found - probably not a feed address"
            dated = [i["published"] for i in items if i["published"]]
            newest = max(dated).strftime("%Y-%m-%d") if dated else "no dates"
            return "OK    %3d items, newest %s%s" % (len(items), newest, method_note(feed["url"]))
        except Exception as err:
            return "FAIL  " + explain(err)

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(test, feeds))
    for feed, result in zip(feeds, results):
        print("%-34s %s" % (feed["label"][:34], result))
    bad = sum(r.startswith("FAIL") for r in results)
    print("\n%d working, %d failing." % (len(results) - bad, bad))


SIGNS = ["captcha", "cf-chl", "just a moment", "enable javascript", "access denied", "datadome",
         "paywall", "subscribe", "abbonati", "abonn", "consent", "cookiewall"]


def diagnose(feeds, max_age):
    """Write diagnostics.txt. Reads feeds.txt only; names.txt is never opened."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age)
    print("Diagnosing %d feeds (two sample articles each) ..." % len(feeds))

    def one(feed):
        out = ["== %s  [%s]%s" % (feed["label"], feed["section"],
                                  "  (headlines-only)" if feed["headlines_only"] else ""),
               "feed address: " + feed["url"]]
        try:
            data, _ = fetch(feed["url"])
            items = parse_feed(data, feed["url"])
        except Exception as err:
            return out + ["feed: FAIL - " + explain(err), ""]
        dated = sorted(i["published"] for i in items if i["published"])
        recent = [i for i in items if not i["published"] or i["published"] >= cutoff]
        hosts = {}
        for i in items:
            host = urllib.parse.urlsplit(i["url"]).netloc
            hosts[host] = hosts.get(host, 0) + 1
        out.append("feed: OK%s - %d items, %d undated, dated %s to %s, %d within the last %d days" % (
            method_note(feed["url"]), len(items), len(items) - len(dated),
            dated[0].strftime("%Y-%m-%d") if dated else "-", dated[-1].strftime("%Y-%m-%d") if dated else "-",
            len(recent), max_age))
        out.append("articles hosted on: " + ", ".join("%s (%d)" % kv for kv in sorted(hosts.items())))
        rules = {}
        for item in (recent or items)[:2]:
            out.append("sample: " + item["url"])
            out.append("   feed text: %d characters" % len(item["summary"]))
            origin = origin_of(item["url"])
            if origin not in rules:
                rules[origin] = site_rules(origin)
            if rules[origin].unreachable or not rules[origin].can_fetch(ROBOT_NAME, item["url"]):
                out.append("   page: not opened - " + (rules[origin].unreachable or rules[origin].why))
                continue
            try:
                data, charset = fetch(item["url"])
            except Exception as err:
                out.append("   page: FAIL - " + explain(err))
                continue
            markup = decode(data, charset)
            article, everything = page_parts(markup, _PageText.SKIP)
            loose = page_parts(markup, LOOSE_SKIP)[1]
            text = page_text(markup)
            low = markup.lower()
            title = re.search(r"<title[^>]*>(.*?)</title>", markup, re.S | re.I)
            out.append("   page: %d bytes%s; text used: %d characters (inside <article>: %d, whole page: %d, "
                       "whole page incl. menus and forms: %d)" % (len(data), method_note(item["url"]),
                                                                  len(text), len(article), len(everything), len(loose)))
            out.append("   verdict: %s; paywall declared: %s; signs in the page: %s" % (
                "bot check" if is_bot_check(markup, text) else classify(markup, text, item["summary"]),
                "yes" if PAYWALL_FLAG.search(markup) else "no",
                ", ".join(sign for sign in SIGNS if sign in low) or "none"))
            out.append("   title: " + squash(html.unescape(title.group(1)))[:120] if title else "   title: none")
            out.append("   starts: " + text[:160])
        return out + [""]

    with ThreadPoolExecutor(WORKERS) as pool:
        blocks = list(pool.map(one, feeds))
    lines = ["Earshot diagnostics, %s" % now.astimezone().strftime("%d %b %Y %H:%M"),
             "Python %s, curl %s" % (sys.version.split()[0], "available" if CURL else "not found"), ""]
    for block in blocks:
        lines.extend(block)
    if os.path.exists(DB_FILE):
        db = open_db()
        lines.append("== Reasons recorded for unreachable articles in past runs")
        rows = db.execute("""SELECT feed, COALESCE(detail, 'no reason recorded (run before this version)'),
                             COUNT(*) FROM articles WHERE body = 'failed' GROUP BY 1, 2 ORDER BY 1, 3 DESC""").fetchall()
        lines.extend("%s: %s x %d" % row for row in rows)
        if not rows:
            lines.append("none")
        db.commit()
        db.close()
    with open(DIAG_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("Written: " + DIAG_FILE)


def audit(feeds, max_age):
    """Re-read every recent article of every feed and report how much is readable.

    Reads feeds.txt only. names.txt is never opened and the database is not touched.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age)
    print("Audit: downloading %d feeds ..." % len(feeds))

    def get_feed(feed):
        try:
            data, _ = fetch(feed["url"])
            items = parse_feed(data, feed["url"])
            return items, ("ok" if items else "no items found - probably not a feed address")
        except Exception as err:
            return [], explain(err)

    with ThreadPoolExecutor(WORKERS) as pool:
        feed_results = list(pool.map(get_feed, feeds))
    queue, seen, info = [], set(), {}
    for feed, (items, status) in zip(feeds, feed_results):
        dated = sorted(i["published"] for i in items if i["published"])
        recent = [i for i in items if not i["published"] or i["published"] >= cutoff]
        info[feed["label"]] = {"status": status, "items": len(items), "recent": len(recent),
                               "newest": dated[-1].strftime("%Y-%m-%d") if dated else "undated",
                               "method": method_note(feed["url"])}
        for item in recent[:MAX_PER_FEED]:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            item["feed"] = feed
            queue.append(item)
    print("Audit: reading %d articles ..." % len(queue))
    results = read_all(queue)

    names = [("ok", "full-length text"), ("paywalled", "paywall teaser only"), ("thin", "short page"),
             ("challenge", "bot check instead of article"), ("blocked", "not opened: site rules"),
             ("failed", "refused or unreachable"), ("skipped", "not opened: headlines-only setting")]
    lines = ["Earshot coverage audit, %s" % now.astimezone().strftime("%d %b %Y %H:%M"),
             "Every item from the last %d days in each feed (at most %d per feed) was requested once." % (max_age, MAX_PER_FEED),
             "'full-length text' = at least %d characters came back and the page was not a declared-paywall "
             "teaser (under %d characters) or a bot check. It is an estimate, not proof that nothing is cut." % (THIN_BODY, PAYWALL_TEASER),
             ""]
    with open(AUDIT_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        out = csv.writer(fh)
        out.writerow(["outlet", "press group", "link", "result", "characters on page", "characters in feed",
                      "text taken from", "detail"])
        for item, r in zip(queue, results):
            out.writerow([item["feed"]["label"], item["feed"]["section"], item["url"], dict(names)[r["state"]],
                          r["chars"], len(item["summary"]), r["source"], r["detail"]])
    for feed in feeds:
        meta = info[feed["label"]]
        lines.append("== %s  [%s]" % (feed["label"], feed["section"]))
        if meta["status"] != "ok":
            lines += ["feed: FAIL - " + meta["status"], ""]
            continue
        lines.append("feed: OK%s - %d items, newest %s, %d from the last %d days" % (
            meta["method"], meta["items"], meta["newest"], meta["recent"], max_age))
        mine = [(i, r) for i, r in zip(queue, results) if i["feed"] is feed]
        counts = {}
        for _, r in mine:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        lines.append("articles requested: %d | " % len(mine) + " | ".join(
            "%s: %d" % (label, counts[state]) for state, label in names if counts.get(state)))
        full = sorted(r["chars"] for _, r in mine if r["state"] == "ok")
        if full:
            lines.append("full-length pages: %d to %d characters, median %d; text taken from the article tag in %d of %d" % (
                full[0], full[-1], full[len(full) // 2],
                sum(1 for _, r in mine if r["state"] == "ok" and r["source"] == "article tag"), len(full)))
            lines.append("feed itself carries long text (%d+ characters) in %d of %d items" % (
                THIN_BODY, sum(1 for i, _ in mine if len(i["summary"]) >= THIN_BODY), len(mine)))
        reasons = {}
        for _, r in mine:
            if r["state"] in ("failed", "blocked", "challenge"):
                reasons[r["detail"]] = reasons.get(r["detail"], 0) + 1
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append("   reason x %d: %s" % (n, reason))
        lines.append("")
    lines.append("== Sites that refused a page after answering earlier (one more try after a %d-second wait)" % PAUSE)
    waited = ["%s: worked after waiting %d times, still refused %d times" % (
              host, st.get("pause_ok", 0), st.get("pause_failed", 0))
              for host, st in sorted(_host_state.items()) if st.get("pause_ok") or st.get("pause_failed")]
    lines += waited or ["none"]
    with open(AUDIT_TXT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("Written: " + AUDIT_TXT)


def run(args):
    started = datetime.now(timezone.utc)
    stamp = started.isoformat(timespec="milliseconds")
    if args.check_model:
        check_model(load_settings())
        return
    if args.judge_stored:
        judge_stored(load_settings())
        return
    feeds = load_feeds()
    if not feeds:
        sys.exit("feeds.txt has no feeds in it.")
    if args.check_feeds:
        check_feeds(feeds)
        return
    if args.diagnose:
        diagnose(feeds, args.max_age)
        return
    if args.audit:
        audit(feeds, args.max_age)
        return
    judge = make_judge(load_settings())   # stops here, before anything is sent, if the address is not allowed

    if not os.path.exists(NAMES_FILE):
        with open(NAMES_FILE, "w", encoding="utf-8") as fh:
            fh.write(NAMES_TEMPLATE)
        sys.exit("names.txt did not exist, so an empty one was created next to the script.\n"
                 "Open it, add one person per line, and run again.")
    people, warnings = load_names()
    if not people:
        sys.exit("names.txt has no names in it yet. Add one person per line and run again.")
    matcher = Matcher(people)
    print("%d people, %d name forms, %d feeds." % (len(people), len(matcher.lookup), len(feeds)))
    for w in warnings:
        print("  ! " + w)
    if judge:
        print("Language-model step is on: %s." % judge.label)

    db = open_db()
    cutoff = started - timedelta(days=args.max_age)

    # retention: do not keep records about named people longer than needed
    old = (started - timedelta(days=args.keep_days)).isoformat(timespec="seconds")
    housekeeping = (started - timedelta(days=max(90, args.max_age * 2))).isoformat(timespec="seconds")
    db.execute("DELETE FROM matches WHERE found_at < ?", (old,))
    db.execute("DELETE FROM articles WHERE first_seen < ?", (housekeeping,))
    db.execute("DELETE FROM coverage WHERE run_at < ?", (housekeeping,))
    db.execute("DELETE FROM runs WHERE run_at < ?", (old,))
    gone = forget_unlisted(db, people)      # someone removed from names.txt: their records go too
    if gone:
        print("Deleted %d stored match%s for people no longer in names.txt." % (gone, "" if gone == 1 else "es"))

    # 1. download every feed
    print("Downloading feeds ...")

    def get_feed(feed):
        try:
            data, _ = fetch(feed["url"])
            items = parse_feed(data, feed["url"])
            return items, ("ok" if items else "no items found - probably not a feed address")
        except Exception as err:
            return [], explain(err)

    with ThreadPoolExecutor(WORKERS) as pool:
        feed_results = list(pool.map(get_feed, feeds))

    # 2. keep what has not been seen before
    queue, stats, queued = [], {}, set()
    steps = {"items": 0, "old": 0, "seen": 0, "cap": 0, "retry": 0, "texts": 0}
    for feed, (items, status) in zip(feeds, feed_results):
        stat = stats[feed["label"]] = {"section": feed["section"], "status": status,
                                       "new": 0, "ok": 0, "thin": 0, "paywalled": 0,
                                       "failed": 0, "blocked": 0, "items": len(items),
                                       "how": method_note(feed["url"]).strip(" ()")}
        if status != "ok":
            print("  ! %s: %s" % (feed["label"], status))
        for item in items:
            if item["url"] in queued:
                continue                     # the same article listed by two feeds
            steps["items"] += 1
            if item["published"] and item["published"] < cutoff:
                steps["old"] += 1
                continue
            seen = db.execute("SELECT body, attempts FROM articles WHERE url = ?",
                              (item["url"],)).fetchone()
            if seen and not (seen[0] == "failed" and (seen[1] or 1) < MAX_ATTEMPTS):
                steps["seen"] += 1
                continue
            if stat["new"] >= MAX_PER_FEED:
                steps["cap"] += 1            # left for the next run
                continue
            item["retry"] = bool(seen)       # unreachable last time: try again
            steps["retry"] += int(item["retry"])
            queued.add(item["url"])
            item["feed"] = feed
            stat["new"] += 1
            queue.append(item)

    # 3. download every new article page (all of them, matching or not)
    full_text = not args.headlines_only
    print("%d new articles.%s" % (len(queue), " Reading them ..." if full_text and queue else ""))
    bodies = read_all(queue, full_text)

    # 4. match locally
    new_matches, to_judge = 0, []
    for item, read in zip(queue, bodies):
        state, body, detail = read["state"], read["text"], read["detail"]
        feed = item["feed"]
        counted = "failed" if state == "challenge" else state    # a bot check counts as not reachable
        if counted in stats[feed["label"]]:
            stats[feed["label"]][counted] += 1
        published = (item["published"] or started).isoformat(timespec="seconds")
        if item["retry"]:
            db.execute("UPDATE articles SET body = ?, detail = ?, checked_at = ?, feed = ?, "
                       "attempts = COALESCE(attempts, 1) + 1 WHERE url = ?",
                       (state, detail, stamp, feed["label"], item["url"]))
        else:
            db.execute("INSERT OR IGNORE INTO articles (url, feed, title, published, first_seen, "
                       "body, detail, attempts, checked_at) VALUES (?,?,?,?,?,?,?,1,?)",
                       (item["url"], feed["label"], item["title"], published, stamp, state, detail, stamp))
        hits = {}
        for place, text in (("headline", item["title"]), ("byline", item["author"]),
                            ("summary", item["summary"]), ("article text", body)):
            steps["texts"] += 1 if text else 0
            for person, (group, snippet, wide) in matcher.find(text).items():
                entry = hits.setdefault(person, {"group": group, "places": [], "snippet": snippet, "wide": wide})
                entry["places"].append(place)
        for person, entry in hits.items():
            cur = db.execute(
                "INSERT OR IGNORE INTO matches (url, person, grp, places, snippet, feed, section, title, "
                "published, found_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item["url"], person, entry["group"], ", ".join(entry["places"]),
                 entry["snippet"], feed["label"], feed["section"], item["title"],
                 published, stamp))
            new_matches += cur.rowcount
            if cur.rowcount and judge:
                to_judge.append((item["url"], person, feed["label"], item["title"], entry["wide"]))

    judged = {}
    if to_judge:
        print("Asking the model about %d new matches ..." % len(to_judge))
        judged = judge_matches(db, judge, to_judge, matcher.notes)

    for label, s in stats.items():
        db.execute("INSERT INTO coverage (run_at, feed, section, status, new_items, read_ok, thin, "
                   "failed, blocked, paywalled, items, how) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (stamp, label, s["section"], s["status"], s["new"], s["ok"], s["thin"],
                    s["failed"], s["blocked"], s["paywalled"], s["items"], s["how"]))
    took = (datetime.now(timezone.utc) - started).total_seconds()
    db.execute("INSERT OR REPLACE INTO runs (run_at, version, people, name_forms, feeds_total, feeds_ok, "
               "items_in_feeds, skipped_old, skipped_seen, skipped_cap, queued_new, queued_retry, "
               "texts_checked, new_matches, seconds, judged, judge) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (stamp, __version__, len(people), len(matcher.lookup), len(feeds),
                sum(1 for s in stats.values() if s["status"] == "ok"), steps["items"], steps["old"],
                steps["seen"], steps["cap"], len(queue) - steps["retry"], steps["retry"],
                steps["texts"], new_matches, took, len(to_judge) if judge else None,
                judge.label if judge else None))
    db.commit()

    write_csv(db)
    write_report(db, stamp, args.days, len(people))
    db.close()
    print("\n%d new match%s. Report: %s  (%.0f s)" % (
        new_matches, "" if new_matches == 1 else "es", REPORT_FILE, took))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def plain(snippet):
    return snippet.replace(HIT_START, "").replace(HIT_END, "")


def write_csv(db):
    rows = db.execute("""SELECT published, person, grp, feed, section, title, url, places,
                         snippet, found_at, verdict, reason, judge FROM matches
                         ORDER BY published DESC""").fetchall()
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as fh:
        out = csv.writer(fh)
        out.writerow(["published", "person", "group", "outlet", "press group", "headline",
                      "link", "found in", "context", "found at", "model verdict", "model reason", "judged by"])
        for r in rows:
            out.writerow(list(r[:8]) + [plain(r[8]), r[9], VERDICTS.get(r[10], r[10] or ""), r[11] or "", r[12] or ""])


def local_day(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y")
    except Exception:
        return iso[:10]


OUTCOMES = {"ok": "read in full", "paywalled": "paywall teaser only", "thin": "short page",
            "challenge": "bot check instead of the article", "blocked": "not opened: site rules",
            "failed": "refused or unreachable", "skipped": "not opened: headlines-only setting"}


def write_report(db, stamp, days, people_count):
    esc = html.escape
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.execute("""SELECT m.published, m.person, m.grp, m.feed, m.section, m.title, m.url, m.places,
                         m.snippet, m.found_at, a.body, a.checked_at, m.verdict, m.reason, m.judge FROM matches m
                         LEFT JOIN articles a ON a.url = m.url WHERE m.published >= ?
                         ORDER BY m.published DESC""", (since,)).fetchall()
    coverage = db.execute("""SELECT feed, section, status, new_items, read_ok, thin, failed, blocked,
                             COALESCE(paywalled, 0), items, how FROM coverage WHERE run_at = ?
                             ORDER BY section, feed""", (stamp,)).fetchall()
    run = db.execute("SELECT * FROM runs WHERE run_at = ?", (stamp,)).fetchone()
    read_now = db.execute("""SELECT feed, title, url, body, detail, attempts FROM articles
                             WHERE checked_at = ? ORDER BY feed, body, title""", (stamp,)).fetchall()
    history = db.execute("""SELECT r.run_at, r.queued_new + r.queued_retry, r.new_matches, r.seconds,
                            r.feeds_ok, r.feeds_total,
                            (SELECT SUM(read_ok) FROM coverage c WHERE c.run_at = r.run_at),
                            (SELECT SUM(paywalled) FROM coverage c WHERE c.run_at = r.run_at),
                            (SELECT SUM(failed) FROM coverage c WHERE c.run_at = r.run_at),
                            (SELECT SUM(blocked) FROM coverage c WHERE c.run_at = r.run_at)
                            FROM runs r ORDER BY r.run_at DESC LIMIT 30""").fetchall()
    groups = sorted({r[2] for r in rows if r[2]})

    def when(iso, fmt="%d %b %Y, %H:%M"):
        try:
            return datetime.fromisoformat(iso).astimezone().strftime(fmt)
        except Exception:
            return iso or ""

    def safe_link(url):
        return esc(url, quote=True) if url.lower().startswith(("http://", "https://")) else "#"

    body = []
    for (pub, person, grp, feed, section, title, url, places, snippet, found_at, state, checked,
         verdict, reason, judged_by) in rows:
        snip = esc(snippet).replace(HIT_START, "<mark>").replace(HIT_END, "</mark>")
        is_new = ' <span class="new">new</span>' if found_at == stamp else ""
        trail = "found in: %s" % esc(places)
        if state:
            trail += " &middot; page: %s" % esc(OUTCOMES.get(state, state))
        if checked or found_at:
            trail += " &middot; checked %s" % esc(when(checked or found_at))
        model = ""
        if verdict:
            model = '<div class="m"><span class="v">Model: %s</span> %s <span class="g">(%s)</span></div>' % (
                esc(VERDICTS.get(verdict, verdict)), esc(reason or ""), esc(judged_by or ""))
        body.append(
            '<tr data-group="%s" data-verdict="%s"><td class="d">%s</td><td><b>%s</b><br><span class="g">%s</span></td>'
            '<td>%s<br><span class="g">%s</span></td>'
            '<td><a href="%s" rel="noopener noreferrer">%s</a>%s'
            '<div class="s">%s</div>%s<span class="g">%s</span></td></tr>'
            % (esc(grp or "", quote=True), esc(verdict or "", quote=True), esc(local_day(pub)), esc(person),
               esc(grp or ""), esc(feed), esc(section or ""), safe_link(url), esc(title or url), is_new, snip,
               model, trail))

    feeds_rows, cov = [], []
    for feed, section, status, new, ok, thin, failed, blocked, paywalled, items, how in coverage:
        answer = "answered" + (" (%s)" % esc(how) if how else "") if status == "ok" \
            else '<span class="bad">failed: %s</span>' % esc(status)
        feeds_rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(feed), esc(section or ""), answer, "" if items is None else items))
        cov.append("<tr><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td></tr>"
                   % (esc(feed), esc(section or ""), new, ok, paywalled, thin, blocked or 0, failed))
    pages = ["<tr><td>%s</td><td><a href=\"%s\" rel=\"noopener noreferrer\">%s</a></td><td>%s</td><td>%s%s</td></tr>" % (
        esc(feed or ""), safe_link(url), esc(title or url), esc(OUTCOMES.get(state, state or "")),
        esc(detail or ""), " (attempt %d)" % attempts if attempts and attempts > 1 else "")
        for feed, title, url, state, detail, attempts in read_now]
    hist = ["<tr><td>%s</td><td>%s of %s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%.0f s</td></tr>" % (
        esc(when(h[0])), h[4], h[5], h[1] or 0, h[6] or 0, h[7] or 0, h[9] or 0, h[8] or 0, h[2] or 0, h[3] or 0)
        for h in history]

    buttons = '<button type="button" class="on" aria-pressed="true" data-g="">All</button>' + "".join(
        '<button type="button" aria-pressed="false" data-g="%s">%s</button>' % (esc(g, quote=True), esc(g))
        for g in groups)
    new_count = sum(1 for r in rows if r[9] == stamp)
    hide = ""
    if any(r[12] for r in rows):
        hide = ('<label class="hide"><input type="checkbox" id="hide"> Hide matches the model thinks are '
                'namesakes (%d)</label>' % sum(1 for r in rows if r[12] == "other"))
    if run:
        (_, version, people, forms, feeds_total, feeds_ok, items, old, seen, cap, q_new, q_retry,
         texts, new_matches, seconds, judged, judge_label) = run
        steps = """<ol class="steps">
<li><b>Feeds downloaded.</b> %d of %d feeds answered.</li>
<li><b>Articles queued.</b> The feeds listed %d articles. Older than the cut-off: %d. Already checked on an earlier run: %d. Left for the next run (limit of %d per feed): %d. Queued now: %d, of which second or third attempts: %d.</li>
<li><b>Pages requested.</b> Every queued article was requested, whether or not it would match. The tables below show what each site answered.</li>
<li><b>Matching, on this computer.</b> %d pieces of text (headlines, bylines, summaries, article texts) were compared with %d people (%d name forms). %d new matches. The list of names is not used in any request to a news site.</li>
%s</ol>""" % (feeds_ok, feeds_total, items, old, seen, MAX_PER_FEED, cap, q_new + q_retry, q_retry,
            texts, people, forms, new_matches,
            "<li><b>Namesake check by a language model.</b> For each of the %d new matches, the person's name and the "
            "text around it were sent to %s. Its verdict and reason are shown with each match; no match is "
            "removed.</li>\n" % (judged or 0, esc(judge_label)) if judge_label else
            "<li><b>Namesake check by a language model.</b> Switched off for this run.</li>\n")
        foot = "Earshot %s &middot; this run took %.0f seconds" % (esc(version or ""), seconds or 0)
    else:
        steps, foot = "", "Earshot %s" % __version__

    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Earshot - %(when)s</title>
<style>
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#5c5b57;--line:#e2dfd8;--edge:#85827b;--acc:#0b5cab;--onacc:#fff;--mark:#ffe58a;--card:#fff;--bad:#a3281c}
@media (prefers-color-scheme:dark){:root{--bg:#17181a;--fg:#e9e7e2;--mut:#aeaca4;--line:#303236;--edge:#8b8d93;--acc:#7db7f5;--onacc:#10161d;--mark:#6b5a12;--card:#1e2023;--bad:#f5a097}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 8px}h3{font-size:15px;margin:22px 0 6px}
.sub{color:var(--mut);margin:0 0 20px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.skip{position:absolute;left:-999px;top:8px;background:var(--card);color:var(--acc);padding:8px 12px;border:2px solid var(--acc);border-radius:6px}
.skip:focus{left:8px}
:focus-visible{outline:3px solid var(--acc);outline-offset:2px}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
input{flex:1;min-width:200px;padding:8px 10px;border:1px solid var(--edge);border-radius:6px;background:var(--card);color:var(--fg);font:inherit}
button{padding:7px 12px;border:1px solid var(--edge);border-radius:6px;background:var(--card);color:var(--fg);font:inherit;cursor:pointer}
button.on{background:var(--acc);border-color:var(--acc);color:var(--onacc)}
.count{color:var(--mut);font-size:13px;margin:0 0 10px;min-height:1.5em}
.wrap{overflow-x:auto}table{width:100%%;min-width:720px;border-collapse:collapse;background:var(--card);border:1px solid var(--line)}
th,td{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid var(--line)}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
td.d{white-space:nowrap;color:var(--mut)}a{color:var(--acc)}
.g{color:var(--mut);font-size:13px}.s{margin:6px 0 4px;font-size:14px}
mark{background:var(--mark);color:inherit;padding:0 2px;border-radius:2px}
.new{background:var(--acc);color:var(--onacc);font-size:11px;padding:1px 6px;border-radius:9px;margin-left:6px}
.m{margin:4px 0;font-size:14px}.v{font-weight:600;border:1px solid var(--edge);border-radius:4px;padding:0 5px}
.hide{display:block;margin:0 0 8px;font-size:14px}.hide input{flex:none;min-width:0;width:auto;margin-right:6px}
.bad{color:var(--bad)}details{margin-top:14px}summary{cursor:pointer;font-weight:600;margin-bottom:10px}
.steps li{margin-bottom:8px}.empty{padding:30px;text-align:center;color:var(--mut)}
footer{margin-top:40px;color:var(--mut);font-size:13px}
</style></head><body>
<a class="skip" href="#matches">Skip to the matches</a>
<main>
<h1>Earshot</h1>
<p class="sub">Last run %(when)s &middot; %(people)d people watched &middot; %(total)d matches in the last %(days)d days, %(new)d new in this run</p>

<h2 id="matches" tabindex="-1">Matches</h2>
<div class="bar">
<label class="sr" for="q">Filter the matches by name, outlet or word</label>
<input id="q" type="search" placeholder="Filter by name, outlet or word">
<div role="group" aria-label="Show one group" class="bar" style="margin:0">%(buttons)s</div>
</div>
%(hide)s
<p class="count" id="count" role="status" aria-live="polite"></p>
<div class="wrap" role="region" aria-label="Matches" tabindex="0"><table id="t">
<caption class="sr">Matches, newest first</caption>
<thead><tr><th scope="col">Published</th><th scope="col">Person</th><th scope="col">Outlet</th><th scope="col">Article</th></tr></thead>
<tbody>%(rows)s</tbody></table></div>
%(empty)s

<h2>This run, step by step</h2>
%(steps)s
<details><summary>Step 1: what each feed answered</summary>
<div class="wrap" role="region" aria-label="Feeds" tabindex="0"><table>
<caption class="sr">Feeds downloaded in this run</caption>
<thead><tr><th scope="col">Feed</th><th scope="col">Press group</th><th scope="col">Answer</th><th scope="col">Articles listed</th></tr></thead>
<tbody>%(feeds)s</tbody></table></div></details>
<details><summary>Step 3: coverage by outlet</summary>
<p class="g">"Read in full": a full-length text was checked, from the page or from the feed itself. "Paywall": the page declares itself subscriber-only and returned only a teaser (under 2,000 characters), so only that was checked. "Short": little text and no declared paywall - usually a brief item or a video page, sometimes an undeclared paywall. "Site rules": the outlet's robots.txt does not allow automated reading of article pages, so they were not opened. "Not reachable": the site refused, timed out, or answered with a bot check instead of the article; refusals and time-outs are tried again on the next runs. A name deeper in a paywalled, ruled-out or unreachable article will have been missed. "Read in full" is an estimate based on how much text came back.</p>
<div class="wrap" role="region" aria-label="Coverage by outlet" tabindex="0"><table>
<caption class="sr">Coverage of this run, by outlet</caption>
<thead><tr><th scope="col">Feed</th><th scope="col">Press group</th><th scope="col">Requested</th><th scope="col">Read in full</th><th scope="col">Paywall</th><th scope="col">Short</th><th scope="col">Site rules</th><th scope="col">Not reachable</th></tr></thead>
<tbody>%(coverage)s</tbody></table></div></details>
<details><summary>Step 3: every article requested in this run (%(pages_n)d)</summary>
<div class="wrap" role="region" aria-label="Articles requested" tabindex="0"><table>
<caption class="sr">Every article requested in this run, with the site's answer</caption>
<thead><tr><th scope="col">Outlet</th><th scope="col">Article</th><th scope="col">Outcome</th><th scope="col">Detail</th></tr></thead>
<tbody>%(pages)s</tbody></table></div></details>

<h2>Run history</h2>
<div class="wrap" role="region" aria-label="Run history" tabindex="0"><table>
<caption class="sr">The last runs, newest first</caption>
<thead><tr><th scope="col">Run</th><th scope="col">Feeds answering</th><th scope="col">Requested</th><th scope="col">Read in full</th><th scope="col">Paywall</th><th scope="col">Site rules</th><th scope="col">Not reachable</th><th scope="col">New matches</th><th scope="col">Duration</th></tr></thead>
<tbody>%(history)s</tbody></table></div>
<footer>%(foot)s</footer>
</main><script>
var q=document.getElementById('q'),count=document.getElementById('count'),
rows=[].slice.call(document.querySelectorAll('#t tbody tr')),
buttons=[].slice.call(document.querySelectorAll('.bar button')),hide=document.getElementById('hide'),g='';
function apply(){var s=q.value.toLowerCase(),h=hide&&hide.checked,n=0;rows.forEach(function(r){
var show=(!g||r.dataset.group===g)&&(!s||r.textContent.toLowerCase().indexOf(s)>-1)&&!(h&&r.dataset.verdict==='other');
r.hidden=!show;if(show)n++;});
count.textContent=(s||g||h)?n+' of '+rows.length+' matches shown':'';}
q.addEventListener('input',apply);if(hide)hide.addEventListener('change',apply);
buttons.forEach(function(b){b.addEventListener('click',function(){g=b.dataset.g;
buttons.forEach(function(x){x.classList.toggle('on',x===b);x.setAttribute('aria-pressed',x===b?'true':'false');});apply();});});
</script></body></html>
""" % {"when": esc(when(stamp)), "people": people_count, "total": len(rows), "days": days,
       "new": new_count, "buttons": buttons, "hide": hide, "rows": "\n".join(body),
       "empty": "" if rows else '<p class="empty">No matches in this period.</p>',
       "steps": steps, "feeds": "\n".join(feeds_rows), "coverage": "\n".join(cov),
       "pages": "\n".join(pages), "pages_n": len(pages), "history": "\n".join(hist), "foot": foot}
    with open(REPORT_FILE, "w", encoding="utf-8") as fh:
        fh.write(page)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:                                   # a Windows console may not be able to show every character
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Earshot: local press monitoring.")
    ap.add_argument("--version", action="version", version="earshot " + __version__)
    ap.add_argument("--check-feeds", action="store_true", help="test every feed and stop")
    ap.add_argument("--diagnose", action="store_true",
                    help="write diagnostics.txt about every feed and two sample articles each")
    ap.add_argument("--audit", action="store_true",
                    help="re-read every recent article and write coverage_audit.txt and .csv")
    ap.add_argument("--check-model", action="store_true",
                    help="ask the model set in settings.txt an invented question and stop")
    ap.add_argument("--judge-stored", action="store_true",
                    help="ask the model about stored matches it has not judged yet")
    ap.add_argument("--headlines-only", action="store_true",
                    help="match on headline, byline and summary only; do not open article pages")
    ap.add_argument("--days", type=int, default=30, help="days shown in the report (default 30)")
    ap.add_argument("--max-age", type=int, default=14,
                    help="ignore feed items older than this many days (default 14)")
    ap.add_argument("--keep-days", type=int, default=365,
                    help="delete stored matches older than this many days (default 365)")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
