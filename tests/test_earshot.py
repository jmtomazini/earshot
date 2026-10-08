# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 Juliana Tomazini
# Licensed under the EUPL
"""Tests for earshot.py. Standard library only.

Run them from the project folder:

    python3 -m unittest discover -s tests -v        (Windows: py -m unittest discover -s tests -v)

No test touches the internet. Each one starts small web servers on this
computer that play the part of news sites, points the monitor at them, and
checks what the monitor asked for and what it concluded.
"""

import argparse
import collections
import contextlib
import csv
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# requests to the test servers must not be handed to a proxy
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.dont_write_bytecode = True

import earshot as pm  # noqa: E402

FILLER = "<p>" + "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 40 + "</p>"
NAMES = """# test list
[GGP]
Zoë Testperson | Zoe Testperson
Đorđe Primjerović | Djordje Primjerovic
[EGPP]
Jan van der Beispiel
Ana O'Sample-Smith
Nobody Atall
"""
NAME_FRAGMENTS = ["testperson", "primjerovi", "djordje", "dorde", "beispiel", "sample-smith",
                  "o'sample", "atall", "zoe", "zoë"]


def ago(hours=1.0, days=0):
    return format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours, days=days))


def rss(items):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" '
           'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Mock</title>']
    for i in items:
        out.append("<item><title><![CDATA[%s]]></title><link>%s</link><pubDate>%s</pubDate>"
                   "<description><![CDATA[%s]]></description>%s%s</item>" % (
                       i.get("title", "Untitled"), i["link"].replace("&", "&amp;"), i.get("date") or ago(),
                       i.get("summary", "A summary."),
                       "<dc:creator>%s</dc:creator>" % i["author"] if i.get("author") else "",
                       "<content:encoded><![CDATA[%s]]></content:encoded>" % i["content"] if i.get("content") else ""))
    out.append("</channel></rss>")
    return "\n".join(out)


def page(body, head="", article=True):
    inner = "<h1>Headline</h1>%s%s" % (body, FILLER)
    return ("<html><head><title>t</title><script>var x='Zoë Testperson';</script>%s</head><body>"
            "<nav>Most read: Zoë Testperson wins prize</nav>%s<footer>Zoë Testperson footer</footer>"
            "</body></html>" % (head, "<article>%s</article>" % inner if article else "<div>%s</div>" % inner))


class Site:
    """A pretend news site on this computer that records every request it receives."""

    def __init__(self):
        self.routes = {"/robots.txt": (200, "text/plain", "User-agent: *\nDisallow:\n")}
        self.log = []
        self.hits = collections.Counter()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                site.log.append((self.path, dict(self.headers)))
                site.hits[path] += 1
                route = site.routes.get(path)
                if callable(route):
                    route = route(self, site.hits[path])
                if route is None:
                    route = (404, "text/plain", "not found")
                status, ctype, body = route[:3]
                extra = route[3] if len(route) > 3 else {}
                if isinstance(body, str):
                    body = body.encode(extra.pop("charset", "utf-8"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
                site.posts.append((self.path, dict(self.headers), self.body))
                route = site.routes.get("POST " + self.path)
                if callable(route):
                    route = route(self, len(site.posts))
                status, ctype, body = route or (404, "text/plain", "not found")
                body = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:              # the monitor stopped waiting, as intended
                    pass

            def log_message(self, *args):
                pass

        self.posts = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p.split("?")[0] for p, _ in self.log]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MonitorCase(unittest.TestCase):
    """Gives every test an empty folder, a fresh monitor state and helper methods."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="earshot-test-")
        self.saved = {}
        for name, value in {
                "NAMES_FILE": "names.txt", "FEEDS_FILE": "feeds.txt", "DATA_DIR": "data",
                "DB_FILE": os.path.join("data", "monitor.db"), "REPORT_FILE": "report.html",
                "CSV_FILE": "matches.csv", "DIAG_FILE": "diagnostics.txt",
                "AUDIT_TXT": "coverage_audit.txt", "AUDIT_CSV": "coverage_audit.csv",
                "SETTINGS_FILE": "settings.txt"}.items():
            self.saved[name] = getattr(pm, name)
            setattr(pm, name, os.path.join(self.folder, value))
        for name, value in {"HOST_DELAY": 0.0, "PAUSE": 0.05, "TIMEOUT": 10, "MODEL_TIMEOUT": 10}.items():
            self.saved[name] = getattr(pm, name)
            setattr(pm, name, value)
        for state in (pm._host_locks, pm._host_last, pm._host_state):
            state.clear()
        self.sites = []

    def tearDown(self):
        for site in self.sites:
            site.close()
        for name, value in self.saved.items():
            setattr(pm, name, value)
        shutil.rmtree(self.folder, ignore_errors=True)

    def site(self):
        site = Site()
        self.sites.append(site)
        return site

    def names(self, text=NAMES, encoding="utf-8"):
        with open(pm.NAMES_FILE, "wb") as fh:
            fh.write(text.encode(encoding))

    def feeds(self, *lines):
        with open(pm.FEEDS_FILE, "w", encoding="utf-8") as fh:
            fh.write("[Mock press]\n" + "\n".join(lines) + "\n")

    def run_monitor(self, **flags):
        args = dict(check_feeds=False, diagnose=False, audit=False, headlines_only=False,
                    check_model=False, judge_stored=False, days=30, max_age=14, keep_days=365)
        args.update(flags)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pm.run(argparse.Namespace(**args))
        return out.getvalue()

    def db(self, query, *params):
        with contextlib.closing(sqlite3.connect(pm.DB_FILE)) as db:
            return db.execute(query, params).fetchall()

    def matches(self):
        return {(person, title): places for person, title, places in
                self.db("SELECT person, title, places FROM matches")}

    def outcome(self, site, path):
        rows = self.db("SELECT body, detail, attempts FROM articles WHERE url = ?", site.url + path)
        return rows[0] if rows else None

    def simple_site(self, body="<p>Nothing relevant here.</p>", **item):
        """One site with one feed and one article."""
        site = self.site()
        site.routes["/a"] = (200, "text/html; charset=utf-8", page(body))
        entry = {"title": "A story", "link": site.url + "/a"}
        entry.update(item)
        site.routes["/rss.xml"] = (200, "application/rss+xml", rss([entry]))
        self.feeds("Mock | %s/rss.xml" % site.url)
        return site


# ---------------------------------------------------------------------------
class Matching(unittest.TestCase):
    def setUp(self):
        self.m = pm.Matcher([("Zoë Testperson", "GGP", ["Zoë Testperson", "Zoe Testperson"]),
                             ("Đorđe Primjerović", "GGP", ["Đorđe Primjerović", "Djordje Primjerovic"]),
                             ("Ana O'Sample-Smith", "EGPP", ["Ana O'Sample-Smith"]),
                             ("Jan van der Beispiel", "EGPP", ["Jan van der Beispiel"])])

    def found(self, text):
        return set(self.m.find(text))

    def test_accents_and_capitals_do_not_matter(self):
        for text in ("said Zoë Testperson.", "said Zoe Testperson.", "SAID ZOË TESTPERSON.",
                     "said zoë testperson."):          # last one: accent as a separate character
            self.assertEqual(self.found(text), {"Zoë Testperson"}, text)

    def test_only_the_whole_name_matches(self):
        for text in ("Testperson said", "Zoë said", "Zoë Testpersonal said", "MsZoë Testperson",
                     "Zoë X. Testperson"):
            self.assertEqual(self.found(text), set(), text)

    def test_name_followed_by_punctuation_or_possessive(self):
        for text in ("Zoë Testperson's view", "(Zoë Testperson)", "Zoë Testperson, who", "Zoë Testperson"):
            self.assertEqual(self.found(text), {"Zoë Testperson"}, text)

    def test_other_spelling_points_to_the_same_person(self):
        self.assertEqual(self.found("Djordje Primjerovic spoke"), {"Đorđe Primjerović"})
        self.assertEqual(self.found("Dorde Primjerovic spoke"), {"Đorđe Primjerović"})

    def test_curly_apostrophes_and_dash_variants(self):
        self.assertEqual(self.found("with Ana O’Sample‑Smith today"), {"Ana O'Sample-Smith"})

    def test_several_people_in_one_text(self):
        self.assertEqual(self.found("Jan van der Beispiel met Zoe Testperson"),
                         {"Jan van der Beispiel", "Zoë Testperson"})

    def test_snippet_keeps_the_original_spelling_and_marks_the_name(self):
        group, snippet, wide = self.m.find("Asked about it, Professor ZOË TESTPERSON declined.")["Zoë Testperson"]
        self.assertIn("Professor ZOË TESTPERSON declined", wide)
        self.assertEqual(group, "GGP")
        self.assertIn(pm.HIT_START + "ZOË TESTPERSON" + pm.HIT_END, snippet)


class PageText(unittest.TestCase):
    def test_menus_scripts_and_footers_are_left_out(self):
        text = pm.page_text(page("<p>Body text here.</p>"))
        self.assertIn("Body text here.", text)
        self.assertNotIn("Testperson", text)

    def test_story_outside_the_article_tag_is_still_read(self):
        markup = ("<html><body><article><p>%s</p></article><div><p>%s</p></div></body></html>"
                  % ("Opening line. " * 50, "Later Zoë Testperson is quoted. " * 120))
        text, source = pm.page_text_info(markup)
        self.assertEqual(source, "whole page")
        self.assertIn("Zoë Testperson", text)

    def test_article_tag_used_when_it_holds_the_story(self):
        self.assertEqual(pm.page_text_info(page("<p>Body.</p>"))[1], "article tag")

    def test_unclosed_menu_tag_does_not_swallow_the_page(self):
        markup = "<html><body><nav><ul><li>menu<p>%s</p></body></html>" % ("Story text. " * 2500)
        self.assertGreater(len(pm.page_text(markup)), 10000)

    def test_classification(self):
        flag = '<script type="application/ld+json">{"isAccessibleForFree": "False"}</script>'
        self.assertEqual(pm.classify("", "x" * 5000, ""), "ok")
        self.assertEqual(pm.classify("", "x" * 600, ""), "thin")
        self.assertEqual(pm.classify("", "x" * 600, "y" * 3000), "ok")          # long text came in the feed
        self.assertEqual(pm.classify(flag, "x" * 1400, ""), "paywalled")
        self.assertEqual(pm.classify(flag, "x" * 9000, ""), "ok")              # declared, but long text returned

    def test_bot_check_page_is_recognised(self):
        challenge = "<html><head><title>Client Challenge</title></head><body>Please enable JavaScript.</body></html>"
        self.assertTrue(pm.is_bot_check(challenge, pm.page_text(challenge)))
        normal = page("<p>Please enable JavaScript for comments.</p>")
        self.assertFalse(pm.is_bot_check(normal, pm.page_text(normal)))


class FeedFormats(unittest.TestCase):
    def test_rss_atom_and_rdf_are_read(self):
        when = "2026-10-01T10:00:00Z"
        feeds = {
            "rss": rss([{"title": "T", "link": "http://example.org/a?utm_source=rss&id=7", "author": "A. Writer"}]),
            "atom": '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title>'
                    '<link rel="alternate" href="/a?id=7"/><updated>%s</updated><summary>S</summary>'
                    '<author><name>A. Writer</name></author></entry></feed>' % when,
            "rdf": '<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
                   'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<item><title>T</title><link>http://example.org/a?id=7</link><dc:date>%s</dc:date>'
                   '<dc:creator>A. Writer</dc:creator></item></rdf:RDF>' % when,
        }
        for kind, text in feeds.items():
            items = pm.parse_feed(text.encode("utf-8"), "http://example.org/feed")
            self.assertEqual(len(items), 1, kind)
            self.assertEqual(items[0]["url"], "http://example.org/a?id=7", kind)   # tracking tag removed
            self.assertEqual(items[0]["title"], "T", kind)
            self.assertIn("A. Writer", items[0]["author"], kind)
            self.assertIsNotNone(items[0]["published"], kind)

    def test_links_that_are_not_web_addresses_are_dropped(self):
        items = pm.parse_feed(rss([{"link": "file:///etc/passwd"}, {"link": "javascript:alert(1)"},
                                   {"link": "http://example.org/ok"}]).encode(), "http://example.org/feed")
        self.assertEqual([i["url"] for i in items], ["http://example.org/ok"])


class Files(MonitorCase):
    def test_names_file_saved_by_different_editors(self):
        text = "[GGP]\r\nZoë Testperson | Zoe Testperson\r\nMüller-Ångström\r\n"
        for encoding in ("utf-8", "utf-8-sig", "cp1252", "utf-16"):
            self.names(text, encoding)
            people, _ = pm.load_names()
            self.assertEqual([p[0] for p in people], ["Zoë Testperson", "Müller-Ångström"], encoding)
            self.assertEqual(people[0][1], "GGP", encoding)

    def test_short_single_names_raise_a_warning(self):
        self.names("Lee\nZoë Testperson\n")
        self.assertEqual(len(pm.load_names()[1]), 1)

    def test_feed_list_options_and_bad_lines(self):
        with open(pm.FEEDS_FILE, "w", encoding="utf-8") as fh:
            fh.write("# comment\n[Group A]\nOne | http://example.org/1\nTwo | http://example.org/2 | headlines-only\n"
                     "not an address\n")
        with contextlib.redirect_stdout(io.StringIO()):
            feeds = pm.load_feeds()
        self.assertEqual([(f["label"], f["section"], f["headlines_only"]) for f in feeds],
                         [("One", "Group A", False), ("Two", "Group A", True)])

    def test_missing_names_file_is_created_empty_and_the_run_stops(self):
        self.feeds("Mock | http://127.0.0.1:9/rss.xml")
        with self.assertRaises(SystemExit):
            self.run_monitor()
        self.assertTrue(os.path.exists(pm.NAMES_FILE))
        self.assertEqual(pm.load_names()[0], [])


# ---------------------------------------------------------------------------
class NamesStayHere(MonitorCase):
    """The promise the tool rests on: the list of names is never part of a request."""

    def build(self):
        site = self.site()
        stories = {
            "/a1": "<p>Asked about it, Professor <b>Zoë</b> <b>Testperson</b> said the talks were predictable.</p>",
            "/a2": "<p>Djordje Primjerovic spoke to us. Also quoted: Jan van der Beispiel.</p>",
            "/a3": "<p>Nothing relevant here.</p>",
            "/a4": "<p>Nothing relevant here either.</p>",
        }
        for path, body in stories.items():
            site.routes[path] = (200, "text/html; charset=utf-8", page(body))
        site.routes["/rss.xml"] = (200, "application/rss+xml", rss(
            [{"title": "Story %s" % p, "link": site.url + p} for p in stories] +
            [{"title": "Op-ed", "link": site.url + "/a3", "author": "Ana O'Sample-Smith"}]))
        self.feeds("Mock | %s/rss.xml" % site.url)
        return site

    def test_no_request_contains_a_name(self):
        site = self.build()
        self.names()
        self.run_monitor()
        self.run_monitor(check_feeds=True)
        self.run_monitor(diagnose=True)
        self.run_monitor(audit=True)
        self.assertGreater(len(self.matches()), 2, "the run must have found names, or the test proves nothing")
        self.assertGreater(len(site.log), 10)
        for path, headers in site.log:
            sent = urllib.parse.unquote(path + " " + " ".join("%s: %s" % kv for kv in headers.items())).casefold()
            for fragment in NAME_FRAGMENTS:
                self.assertNotIn(fragment, sent, "a request contained part of a name: %s" % path)

    def test_what_is_requested_does_not_depend_on_the_list(self):
        requested = []
        for names in (NAMES, "Somebody Else\n", "Zoë Testperson\n"):
            self.tearDown()
            self.setUp()
            site = self.build()
            self.names(names)
            self.run_monitor()
            requested.append(sorted(site.paths()))
        self.assertEqual(requested[0], requested[1])
        self.assertEqual(requested[0], requested[2])
        self.assertIn("/a3", requested[0])          # articles with no match are requested too

    def test_audit_and_diagnose_never_open_the_names_or_the_database(self):
        self.build()                                 # no names.txt is written at all
        self.run_monitor(audit=True)
        self.run_monitor(diagnose=True)
        self.run_monitor(check_feeds=True)
        self.assertFalse(os.path.exists(pm.NAMES_FILE))
        self.assertFalse(os.path.exists(pm.DATA_DIR))
        self.assertTrue(os.path.exists(pm.AUDIT_TXT))

    def test_report_loads_nothing_from_the_internet(self):
        site = self.build()
        self.names()
        self.run_monitor()
        with open(pm.REPORT_FILE, encoding="utf-8") as fh:
            report = fh.read()
        self.assertEqual(re.findall(r"<(?:img|link|iframe|object|embed|video|audio|source)\b", report), [])
        self.assertEqual(re.findall(r"<script[^>]+src=", report), [])
        self.assertEqual(re.findall(r"url\(|@import", report), [])
        for address in re.findall(r'href="(https?://[^"]+)"', report):
            self.assertTrue(address.startswith(site.url), address)     # only links to the articles themselves


class Reading(MonitorCase):
    def test_names_found_in_headline_byline_summary_and_text(self):
        site = self.site()
        site.routes["/h"] = (200, "text/html; charset=utf-8", page("<p>Nothing.</p>"))
        site.routes["/b"] = (200, "text/html; charset=utf-8", page("<p>Nothing.</p>"))
        site.routes["/s"] = (200, "text/html; charset=utf-8", page("<p>Nothing.</p>"))
        site.routes["/t"] = (200, "text/html; charset=utf-8", page("<p>JAN VAN DER BEISPIEL disagreed.</p>"))
        site.routes["/l"] = (200, "text/html; charset=iso-8859-1",
                             page("<p>Secondo Zoë Testperson, la situazione è grave.</p>"), {"charset": "iso-8859-1"})
        site.routes["/z"] = (200, "text/html; charset=utf-8",
                             gzip.compress(page("<p>Zoe Testperson, compressed.</p>").encode()),
                             {"Content-Encoding": "gzip"})
        site.routes["/rss.xml"] = (200, "application/rss+xml", rss([
            {"title": "Đorđe Primjerović on trade", "link": site.url + "/h"},
            {"title": "Opinion", "link": site.url + "/b", "author": "Zoe Testperson"},
            {"title": "Summary", "link": site.url + "/s", "summary": "<p>With Ana O’Sample-Smith.</p>"},
            {"title": "Text", "link": site.url + "/t"},
            {"title": "Latin-1", "link": site.url + "/l"},
            {"title": "Compressed", "link": site.url + "/z"}]))
        self.feeds("Mock | %s/rss.xml" % site.url)
        self.names()
        out = self.run_monitor()
        self.assertEqual(self.matches(), {
            ("Đorđe Primjerović", "Đorđe Primjerović on trade"): "headline",
            ("Zoë Testperson", "Opinion"): "byline",
            ("Ana O'Sample-Smith", "Summary"): "summary",
            ("Jan van der Beispiel", "Text"): "article text",
            ("Zoë Testperson", "Latin-1"): "article text",
            ("Zoë Testperson", "Compressed"): "article text"})
        self.assertIn("6 new matches", out)
        with open(pm.CSV_FILE, encoding="utf-8-sig") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 6)
        self.assertIn("0 new matches", self.run_monitor())      # nothing is read or reported twice
        self.assertEqual(site.hits["/t"], 1)

    def test_name_in_a_menu_or_script_is_not_a_match(self):
        self.simple_site()                           # every mock page carries the name in menu, script and footer
        self.names()
        self.run_monitor()
        self.assertEqual(self.matches(), {})

    def test_old_articles_are_skipped_and_the_limit_defers_the_rest(self):
        site = self.site()
        items = [{"title": "Old", "link": site.url + "/old", "date": ago(days=40)}]
        for n in range(5):
            site.routes["/n%d" % n] = (200, "text/html", page("<p>x</p>"))
            items.append({"title": "New %d" % n, "link": site.url + "/n%d" % n})
        site.routes["/rss.xml"] = (200, "application/rss+xml", rss(items))
        self.feeds("Mock | %s/rss.xml" % site.url)
        self.names()
        old_limit, pm.MAX_PER_FEED = pm.MAX_PER_FEED, 3
        try:
            self.run_monitor()
            self.assertEqual(sum(site.hits["/n%d" % n] for n in range(5)), 3)
            self.run_monitor()
            self.assertEqual(sum(site.hits["/n%d" % n] for n in range(5)), 5)
        finally:
            pm.MAX_PER_FEED = old_limit
        self.assertEqual(site.hits["/old"], 0)
        first = self.db("SELECT skipped_old, skipped_cap, queued_new FROM runs ORDER BY run_at")[0]
        self.assertEqual(first, (1, 2, 3))

    def test_headlines_only_feed_never_opens_article_pages(self):
        site = self.simple_site(title="Zoë Testperson appointed")
        self.feeds("Mock | %s/rss.xml | headlines-only" % site.url)
        self.names()
        self.run_monitor()
        self.assertEqual(site.hits["/a"], 0)
        self.assertEqual(self.matches(), {("Zoë Testperson", "Zoë Testperson appointed"): "headline"})

    def test_feed_carrying_the_full_text_counts_as_read_in_full(self):
        site = self.simple_site(content="<p>Jan van der Beispiel is quoted in the feed.</p>" + FILLER)
        site.routes["/a"] = (200, "text/html", "<html><body><article><p>Tiny page.</p></article></body></html>")
        self.names()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "ok")
        self.assertEqual(self.matches(), {("Jan van der Beispiel", "A story"): "summary"})


class SiteAnswers(MonitorCase):
    def test_pages_ruled_out_by_robots_txt_are_not_requested(self):
        site = self.simple_site("<p>Zoë Testperson, private.</p>")
        site.routes["/robots.txt"] = (200, "text/plain", "User-agent: *\nDisallow: /a\n")
        self.names()
        self.run_monitor()
        self.assertEqual(site.hits["/a"], 0)
        self.assertEqual(self.outcome(site, "/a")[:2], ("blocked", "ruled out by robots.txt"))

    def test_refused_robots_txt_sets_no_rules(self):            # RFC 9309, section 2.3.1.3
        site = self.simple_site("<p>Zoë Testperson said so.</p>")
        site.routes["/robots.txt"] = (403, "text/plain", "no")
        self.names()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "ok")
        self.assertEqual(len(self.matches()), 1)

    def test_unreachable_robots_txt_means_do_not_read_but_try_again_later(self):   # RFC 9309, section 2.3.1.4
        site = self.simple_site()
        site.routes["/robots.txt"] = lambda handler, n: (500, "text/plain", "down") if n == 1 else \
            (200, "text/plain", "User-agent: *\nDisallow:\n")
        self.names()
        self.run_monitor()
        self.assertEqual(site.hits["/a"], 0)
        self.assertEqual(self.outcome(site, "/a")[0], "failed")
        for state in (pm._host_locks, pm._host_last, pm._host_state):
            state.clear()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "ok")

    def test_declared_paywall_teaser(self):
        site = self.simple_site()
        site.routes["/a"] = (200, "text/html", '<html><head><script type="application/ld+json">'
                             '{"@type":"NewsArticle","isAccessibleForFree":false}</script></head><body><article>'
                             '<p>%s Zoë Testperson is named in the teaser.</p></article></body></html>' % ("Teaser. " * 150))
        self.names()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "paywalled")
        self.assertEqual(len(self.matches()), 1)                # the teaser itself is still checked

    def test_bot_check_page_is_reported_and_its_text_is_not_matched(self):
        site = self.simple_site()
        site.routes["/a"] = (200, "text/html", "<html><head><title>Client Challenge</title></head><body>"
                             "<noscript>Please enable JavaScript. Zoë Testperson</noscript></body></html>")
        self.names()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "challenge")
        self.assertEqual(self.matches(), {})
        self.assertEqual(self.db("SELECT failed FROM coverage")[0][0], 1)

    def test_unreachable_page_is_tried_three_times_in_all(self):
        site = self.simple_site()
        site.routes["/a"] = (500, "text/plain", "error")
        self.names()
        for _ in range(5):
            self.run_monitor()
        self.assertEqual(site.hits["/a"], pm.MAX_ATTEMPTS)
        self.assertEqual(self.outcome(site, "/a"), ("failed", "HTTP 500", pm.MAX_ATTEMPTS))

    def test_page_that_fails_once_is_read_on_the_next_run(self):
        site = self.simple_site()
        site.routes["/a"] = lambda handler, n: (500, "text/plain", "error") if n == 1 else \
            (200, "text/html; charset=utf-8", page("<p>Đorđe Primjerović, at last.</p>"))
        self.names()
        self.run_monitor()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "ok")
        self.assertEqual(len(self.matches()), 1)

    def test_site_that_dislikes_the_first_identity_accepts_the_plain_one(self):
        site = self.simple_site("<p>Zoë Testperson said so.</p>")
        ok = site.routes["/a"]
        site.routes["/a"] = lambda handler, n: (403, "text/plain", "no") \
            if "compatible" in handler.headers.get("User-Agent", "") else ok
        self.names()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "ok")
        agents = [h.get("User-Agent", "") for p, h in site.log if p == "/a"]
        self.assertTrue(all("earshot" in a for a in agents), agents)     # it always says what it is

    @unittest.skipUnless(pm.CURL, "this computer has no curl program")
    def test_site_that_turns_python_away_is_read_through_curl(self):
        site = self.simple_site()
        body = gzip.compress(page("<p>Zoë Testperson, via curl.</p>").encode())    # compressed without being asked
        site.routes["/a"] = lambda handler, n: (403, "text/plain", "no") \
            if handler.headers.get("Connection", "") == "close" else \
            (200, "text/html; charset=utf-8", body, {"Content-Encoding": "gzip"})
        self.names()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "ok")
        self.assertEqual(len(self.matches()), 1)

    def test_site_that_refuses_everything_is_reported_as_refusing(self):
        site = self.simple_site()
        site.routes["/a"] = (403, "text/plain", "no")
        self.names()
        self.run_monitor()
        state, detail, _ = self.outcome(site, "/a")
        self.assertEqual(state, "failed")
        self.assertIn("403", detail)

    def test_site_asking_to_slow_down_is_given_a_pause(self):
        site = self.site()
        last = [0.0]

        def paced(handler, n):
            if time.time() - last[0] < 0.15:
                return (403, "text/plain", "slow down")
            last[0] = time.time()
            return (200, "text/html", page("<p>x</p>"))

        items = []
        for n in range(4):
            site.routes["/p%d" % n] = paced
            items.append({"title": "P%d" % n, "link": site.url + "/p%d" % n})
        site.routes["/rss.xml"] = (200, "application/rss+xml", rss(items))
        self.feeds("Mock | %s/rss.xml" % site.url)
        self.names()
        pm.PAUSE = 0.25
        self.run_monitor()
        self.assertEqual([r[0] for r in self.db("SELECT body FROM articles")], ["ok"] * 4)

    def test_dead_feed_and_non_feed_are_reported(self):
        site = self.site()
        site.routes["/page.html"] = (200, "text/html", "<html><body>Not a feed</body></html>")
        self.feeds("Not a feed | %s/page.html" % site.url, "Dead | http://127.0.0.1:9/rss.xml")
        out = self.run_monitor(check_feeds=True)
        self.assertIn("0 working, 2 failing", out)


class Records(MonitorCase):
    def test_database_from_the_first_version_is_upgraded_in_place(self):
        site = self.simple_site("<p>Zoë Testperson said so.</p>")
        os.makedirs(pm.DATA_DIR)
        with contextlib.closing(sqlite3.connect(pm.DB_FILE)) as db:
            db.executescript("""
                CREATE TABLE articles (url TEXT PRIMARY KEY, feed TEXT, title TEXT, published TEXT,
                                       first_seen TEXT, body TEXT);
                CREATE TABLE matches (url TEXT, person TEXT, grp TEXT, places TEXT, snippet TEXT, feed TEXT,
                                      section TEXT, title TEXT, published TEXT, found_at TEXT,
                                      PRIMARY KEY (url, person));
                CREATE TABLE coverage (run_at TEXT, feed TEXT, section TEXT, status TEXT, new_items INTEGER,
                                       read_ok INTEGER, thin INTEGER, failed INTEGER, blocked INTEGER);""")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.execute("INSERT INTO articles VALUES (?,?,?,?,?,?)", (site.url + "/a", "Old label", "A story", now, now, "failed"))
            db.commit()
        self.names()
        self.run_monitor()
        self.assertEqual(self.outcome(site, "/a")[0], "ok")      # the old failure was retried
        self.assertEqual(len(self.matches()), 1)
        self.assertEqual(len(self.db("SELECT * FROM runs")), 1)

    def test_removing_a_person_from_the_list_removes_their_matches(self):
        site = self.site()
        site.routes["/z"] = (200, "text/html; charset=utf-8", page("<p>Zoë Testperson said so.</p>"))
        site.routes["/j"] = (200, "text/html; charset=utf-8", page("<p>Jan van der Beispiel said so.</p>"))
        site.routes["/rss.xml"] = (200, "application/rss+xml", rss([
            {"title": "One", "link": site.url + "/z"}, {"title": "Two", "link": site.url + "/j"}]))
        self.feeds("Mock | %s/rss.xml" % site.url)
        self.names()
        self.run_monitor()
        self.assertEqual({p for p, _ in self.matches()}, {"Zoë Testperson", "Jan van der Beispiel"})
        self.names("Jan van der Beispiel\n")
        out = self.run_monitor()
        self.assertIn("Deleted 1 stored match", out)
        self.assertEqual({p for p, _ in self.matches()}, {"Jan van der Beispiel"})
        with open(pm.REPORT_FILE, encoding="utf-8") as fh:
            self.assertNotIn("Testperson", fh.read())
        with open(pm.CSV_FILE, encoding="utf-8-sig") as fh:
            self.assertNotIn("Testperson", fh.read())

    def test_old_matches_are_deleted(self):
        site = self.simple_site("<p>Zoë Testperson said so.</p>")
        self.names()
        self.run_monitor()
        self.assertEqual(len(self.matches()), 1)
        with contextlib.closing(sqlite3.connect(pm.DB_FILE)) as db:
            db.execute("UPDATE matches SET found_at = ?", ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(),))
            db.commit()
        self.run_monitor()
        self.assertEqual(self.matches(), {})

    def test_report_shows_each_step_and_is_labelled_for_screen_readers(self):
        site = self.simple_site("<p>Zoë Testperson said so.</p>")
        self.names()
        self.run_monitor()
        with open(pm.REPORT_FILE, encoding="utf-8") as fh:
            report = fh.read()
        for expected in ('<html lang="en">', '<label class="sr" for="q">', 'aria-live="polite"', 'aria-pressed="true"',
                         "<caption", 'scope="col"', 'href="#matches"', "<mark>Zoë Testperson</mark>",
                         "Feeds downloaded.", "Articles queued.", "Pages requested.", "Matching, on this computer.",
                         "Run history", "read in full", site.url + "/a"):
            self.assertIn(expected, report)
        self.assertEqual(len(re.findall(r"<th(?![^>]*scope=)[ >]", report)), 0)   # every header cell says what it heads
        self.assertNotIn("\x01", report)

    def test_coverage_audit_lists_every_article_with_its_outcome(self):
        site = self.simple_site()
        site.routes["/a"] = (403, "text/plain", "no")
        self.run_monitor(audit=True)
        with open(pm.AUDIT_CSV, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([(r["link"], r["result"]) for r in rows], [(site.url + "/a", "refused or unreachable")])


class ModelStep(MonitorCase):
    """The optional step that asks a language model whether a match is a namesake."""

    def model(self, answer=None):
        """A pretend model server on this computer that speaks the OpenAI chat format."""
        site = self.site()

        def reply(handler, n):
            question = json.loads(handler.body)["messages"][-1]["content"]
            content = answer(question) if callable(answer) else (answer or json.dumps(
                {"verdict": "different_person" if "bakery" in question else "same_person",
                 "reason": "the excerpt mentions a bakery" if "bakery" in question else "the excerpt names the EUI"}))
            return (200, "application/json", json.dumps({"choices": [{"message": {"content": content}}]}))

        site.routes["POST /v1/chat/completions"] = reply
        return site

    def settings(self, address, **more):
        lines = ["model_address = %s" % address, "model_name = mock-model",
                 "context = academics at the European University Institute"]
        lines += ["%s = %s" % kv for kv in more.items()]
        with open(pm.SETTINGS_FILE, "w", encoding="utf-8") as fh:
            fh.write("# test settings\n" + "\n".join(lines) + "\n")

    def news(self):
        site = self.site()
        site.routes["/eui"] = (200, "text/html; charset=utf-8",
                               page("<p>Zoë Testperson, a professor at the EUI, said the talks failed.</p>"))
        site.routes["/bakery"] = (200, "text/html; charset=utf-8",
                                  page("<p>Zoë Testperson has run the family bakery for thirty years.</p>"))
        site.routes["/rss.xml"] = (200, "application/rss+xml", rss([
            {"title": "Summit", "link": site.url + "/eui"}, {"title": "Award", "link": site.url + "/bakery"}]))
        self.feeds("Mock | %s/rss.xml" % site.url)
        return site

    def verdicts(self):
        return {title: (verdict, reason) for title, verdict, reason in
                self.db("SELECT title, verdict, reason FROM matches")}

    def test_step_is_off_unless_settings_name_a_model(self):
        self.news()
        model = self.model()
        self.names()
        self.run_monitor()
        self.assertEqual(model.posts, [])
        self.assertEqual(self.verdicts(), {"Summit": (None, None), "Award": (None, None)})

    def test_verdicts_are_stored_and_shown_and_no_match_is_removed(self):
        self.news()
        model = self.model()
        self.settings(model.url + "/v1")
        self.names("Zoë Testperson | Zoe Testperson :: political scientist\n")
        self.run_monitor()
        self.assertEqual(self.verdicts(), {"Summit": ("same", "the excerpt names the EUI"),
                                           "Award": ("other", "the excerpt mentions a bakery")})
        with open(pm.REPORT_FILE, encoding="utf-8") as fh:
            report = fh.read()
        for expected in ("Model: likely a namesake", "Model: likely the listed person", "mock-model at this computer",
                         'data-verdict="other"', 'id="hide"', "Namesake check by a language model."):
            self.assertIn(expected, report)
        with open(pm.CSV_FILE, encoding="utf-8-sig") as fh:
            self.assertEqual(sorted(r["model verdict"] for r in csv.DictReader(fh)),
                             ["likely a namesake", "likely the listed person"])
        sent = json.loads(model.posts[0][2])
        self.assertEqual(sent["model"], "mock-model")
        self.assertIn("political scientist", sent["messages"][-1]["content"])          # the note from names.txt
        self.assertIn("European University Institute", sent["messages"][-1]["content"])  # the general context

    def test_news_sites_still_receive_no_names_when_the_step_is_on(self):
        news = self.news()
        model = self.model()
        self.settings(model.url + "/v1")
        self.names()
        self.run_monitor()
        self.assertEqual(len(model.posts), 2)                    # the model did receive them: that is the step
        for path, headers in news.log:
            sent = urllib.parse.unquote(path + " " + " ".join("%s: %s" % kv for kv in headers.items())).casefold()
            for fragment in NAME_FRAGMENTS:
                self.assertNotIn(fragment, sent)
        self.assertEqual(news.posts, [])

    def test_model_on_another_computer_is_refused_before_anything_is_sent(self):
        news = self.news()
        self.settings("https://models.example.org/v1")
        self.names()
        with self.assertRaises(SystemExit) as stop:
            self.run_monitor()
        self.assertIn("not on this computer", str(stop.exception))
        self.assertEqual(news.log, [])                           # the run stopped before any request at all
        with self.assertRaises(ValueError):
            pm.Judge({"model_address": "http://192.168.1.20:11434/v1", "model_name": "m", "model_key": "",
                      "allow_remote_model": "no", "context": ""})
        allowed = pm.Judge({"model_address": "https://models.example.org/v1", "model_name": "m", "model_key": "k",
                            "allow_remote_model": "yes", "context": ""})
        self.assertFalse(allowed.local)

    def test_cloud_model_behind_a_local_address_is_refused(self):
        news = self.news()
        model = self.model()
        for name in ("gemma4:31b-cloud", "glm-5.3:cloud", "deepseek-v4.1-flash:cloud", "gemma4:cloud"):
            self.settings(model.url + "/v1")
            with open(pm.SETTINGS_FILE, "a", encoding="utf-8") as fh:
                fh.write("model_name = %s\n" % name)
            self.names()
            with self.assertRaises(SystemExit) as stop:
                self.run_monitor()
            self.assertIn("cloud model", str(stop.exception), name)
        self.assertEqual(news.log, [])
        self.assertEqual(model.posts, [])
        for name in ("qwen3:14b", "gemma4:26b", "cloudberry-7b", "gemma4:12b-mlx"):
            judge = pm.Judge({"model_address": "http://localhost:11434/v1", "model_name": name, "model_key": "",
                              "allow_remote_model": "no", "context": ""})
            self.assertTrue(judge.local, name)

    def test_unreachable_model_never_drops_a_match(self):
        self.news()
        self.settings("http://127.0.0.1:9/v1")
        self.names()
        self.run_monitor()
        verdicts = self.verdicts()
        self.assertEqual(len(verdicts), 2)
        self.assertEqual({v for v, _ in verdicts.values()}, {"not judged"})

    def test_a_slow_answer_does_not_stop_the_other_matches_being_judged(self):
        self.news()
        model = self.model()
        fast = model.routes["POST /v1/chat/completions"]

        def slow_then_fast(handler, n):
            if n <= 2:
                time.sleep(1.5)                      # longer than the test's time limit
            return fast(handler, n)

        model.routes["POST /v1/chat/completions"] = slow_then_fast
        pm.MODEL_TIMEOUT = 0.5
        self.settings(model.url + "/v1")
        self.names()
        self.run_monitor()                           # two matches: both slow, both "not judged"
        reasons = {r for _, r in self.verdicts().values()}
        self.assertTrue(all("did not answer within" in r for r in reasons), reasons)
        self.run_monitor(judge_stored=True)          # now fast: both judged, nothing skipped
        self.assertEqual({v for v, _ in self.verdicts().values()}, {"same", "other"})

    def test_thinking_off_adds_the_switch_to_the_question(self):
        self.news()
        model = self.model()
        self.settings(model.url + "/v1", thinking="off")
        self.names()
        self.run_monitor()
        self.assertTrue(all(json.loads(body)["messages"][-1]["content"].endswith("/no_think")
                            for _, _, body in model.posts))

    def test_answers_the_script_cannot_read_become_unclear(self):
        self.assertEqual(pm.parse_verdict("I think it is her."), ("unclear", "the model's answer could not be read"))
        self.assertEqual(pm.parse_verdict('<think>bakery... {"verdict": "same_person"}</think>'
                                          '{"verdict": "different_person", "reason": "a baker"}'), ("other", "a baker"))
        self.assertEqual(pm.parse_verdict('```json\n{"verdict": "same_person", "reason": "EUI"}\n```'), ("same", "EUI"))
        self.assertEqual(pm.parse_verdict('{"verdict": "maybe", "reason": "no clue"}'), ("unclear", "no clue"))
        self.assertEqual(pm.parse_verdict(None)[0], "unclear")

    def test_server_without_json_mode_is_asked_again_plainly(self):
        self.news()
        model = self.model()
        plain_reply = model.routes["POST /v1/chat/completions"]
        model.routes["POST /v1/chat/completions"] = lambda handler, n: \
            (400, "application/json", "{}") if "response_format" in handler.body else plain_reply(handler, n)
        self.settings(model.url + "/v1")
        self.names()
        self.run_monitor()
        self.assertEqual({v for v, _ in self.verdicts().values()}, {"same", "other"})

    def test_stored_matches_can_be_judged_later(self):
        self.news()
        self.names()
        self.run_monitor()                                       # step off: nothing judged
        model = self.model()
        self.settings(model.url + "/v1")
        out = self.run_monitor(judge_stored=True)
        self.assertEqual(len(model.posts), 2)
        self.assertEqual({v for v, _ in self.verdicts().values()}, {"same", "other"})
        self.assertIn("likely a namesake: 1", out)

    def test_judging_later_rewrites_a_report_made_before_run_history_existed(self):
        self.news()
        self.names()
        self.run_monitor()
        with contextlib.closing(sqlite3.connect(pm.DB_FILE)) as db:   # as in a database from the first version
            db.execute("DELETE FROM runs")
            db.commit()
        with open(pm.REPORT_FILE, "w", encoding="utf-8") as fh:
            fh.write("old report")
        model = self.model()
        self.settings(model.url + "/v1")
        out = self.run_monitor(judge_stored=True)
        with open(pm.REPORT_FILE, encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("Model: likely a namesake", report)
        self.assertIn("match 2 of 2", out)
        for fragment in NAME_FRAGMENTS:
            self.assertNotIn(fragment, out.casefold())                  # progress lines carry no names

    def test_model_check_sends_an_invented_example_only(self):
        model = self.model()
        self.settings(model.url + "/v1")
        self.names()
        out = self.run_monitor(check_model=True)
        self.assertIn("likely a namesake", out)
        self.assertIn("Maria Example", model.posts[0][2])
        for fragment in NAME_FRAGMENTS:
            self.assertNotIn(fragment, model.posts[0][2].casefold())


if __name__ == "__main__":
    unittest.main(verbosity=2)
