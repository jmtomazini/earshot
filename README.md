# Earshot

Local press monitoring. The names stay on your computer.

A small script that watches a list of news outlets for mentions of named people.
It downloads the outlets' feeds and articles in bulk and does the matching on
your own computer.

Version 1.1.4. One Python file, no packages to install.

## Three rules it is built on

1. **The list of names never leaves the computer.** The script queries no search
   engine and no API. Its only requests to news sites are for feed, robots.txt
   and article addresses, and those are the same whoever is on the list. The one
   exception is the optional language-model step below, which is off unless you
   switch it on and which only accepts a model on this computer unless you
   explicitly allow another.
2. **Every match is linked to its source.** The report gives the article link, the
   text around the name, where in the article it was found and when it was checked.
3. **Every step is visible.** The report shows what each feed answered, which
   articles were queued, what each site returned for each article, and a history
   of past runs.

## Quick start

| | Mac | Windows |
|---|---|---|
| Needs | Python 3.9 or later | Python 3.9 or later (not included in Windows) |
| Run | double-click `Run Earshot.command` | double-click `Run Earshot.bat` |
| Or, in a terminal | `python3 earshot.py` | `py earshot.py` |

On the first run the script creates an empty `names.txt` and stops. Open it, add
one person per line, and run again. The report opens as `report.html`.

```
[Programme A]
Full Name | Other Spelling | Transliterated Spelling
```

Accents and capitals do not matter. Transliterations do: list them as other
spellings. Use full names; a surname alone matches everyone who shares it.

## What a run does

1. **Feeds.** Downloads every feed in `feeds.txt`.
2. **Queue.** Keeps the articles from the last 14 days that it has not checked
   before, up to 80 per feed.
3. **Pages.** Requests every queued article, whether or not it will match, at
   most one request per second per site.
4. **Matching.** Compares headline, byline, summary and article text with the
   names, on this computer, and writes `report.html` and `matches.csv`.

## Commands

| Command | What it does |
|---|---|
| `python3 earshot.py` | A normal run |
| `... --check-feeds` | Tests every feed address; reads no articles |
| `... --audit` | Requests every recent article once and writes `coverage_audit.txt` and `.csv`: how much of each outlet is readable |
| `... --diagnose` | Writes `diagnostics.txt`: what each feed and two sample articles return |
| `... --headlines-only` | Skips article pages; faster, misses quotes |
| `... --days 60` | Shows 60 days in the report instead of 30 |
| `... --keep-days 180` | Deletes stored matches older than 180 days instead of 365 |
| `... --check-model` | Asks the model in `settings.txt` an invented question, to see that it works |
| `... --judge-stored` | Asks the model about stored matches it has not judged yet |

`--check-feeds`, `--audit` and `--diagnose` never open `names.txt` or the database.

## Optional: telling namesakes apart with a language model

Exact-name matching cannot tell your professor from a baker with the same name.
A language model can read the text around the match and say which it probably is.

- **Off by default.** It runs only if `settings.txt` names a model address. Copy
  `settings.example.txt` to `settings.txt` to switch it on.
- **What is sent.** For each new match: the person's name, the optional note
  about them from `names.txt`, the outlet, the headline and about 1,400 characters
  of text around the name. Nothing else, and only to the address in `settings.txt`.
- **Local unless you say otherwise.** An address that is not on this computer is
  refused, and the run stops before anything is sent, unless `settings.txt` also
  says `allow_remote_model = yes`. A local model is never reached through a proxy.
  Ollama "cloud" models (names containing `cloud`) are refused the same way: Ollama
  on the computer forwards them to its own servers.
- **It labels, it does not decide.** Each match shows the model's verdict
  ("likely the listed person", "likely a namesake", "unclear"), its one-sentence
  reason and which model gave it. No match is removed. A box in the report can
  hide the likely namesakes; it is unticked by default.
- **Any OpenAI-compatible endpoint works.** The example settings use
  [Ollama](https://ollama.com) with a Qwen3 model on the same computer. An
  institutional model server is a change of address.
- **If the model cannot be reached,** matches are kept and marked "not judged".

To describe a person for the model, add a note after `::` in `names.txt`:

```
Full Name | Other Spelling :: political scientist at Example University, works on trade
```

How well a given model does this has not been measured. Check its verdicts
against your own before relying on the "hide" box.

## What leaves the computer, and what is kept

**Sent:** requests for the feed addresses in `feeds.txt`, for each site's
`robots.txt`, and for the article addresses the feeds list. Each request carries
the script's identifier (`earshot`). Sites also see the network address the
request comes from, as with any visit.

**Never sent to a news site:** the names, the matches, the report.

**Sent only if you switch on the language-model step:** for each new match, the
person's name, your note about them and the text around the match, to the model
address in `settings.txt`.

**Kept, in this folder only:**

| File | Contents | Kept for |
|---|---|---|
| `names.txt` | The list of people | Until you change it |
| `settings.txt` | Optional: the model address, and a key if that server needs one | Until you change it |
| `data/monitor.db` | Article addresses, headlines and what each site answered | 90 days |
| `data/monitor.db`, `matches.csv`, `report.html` | Matches: person, outlet, link, about 320 characters of context | 365 days |

Article texts are not stored. When a person is removed from `names.txt`, their
stored matches are deleted at the next run. The `.gitignore` keeps all of the
above out of version control.

The names and the matches are personal data about identifiable people. Where
this folder sits decides who else can reach them: a folder synced to a cloud
drive is stored there too.

## How it treats the sites

- It says what it is: the identifier contains `earshot` in every request.
- It reads a site's `robots.txt` and does not open article pages the file rules
  out. It follows RFC 9309: a robots.txt that answers with a 4xx code sets no
  rules; one that cannot be reached means "do not read" for that run.
- Feeds are read without consulting robots.txt, as feed readers do.
- When a site answers 403, it tries again with a plainer identifier, then through
  the system's `curl` program, then once more after a six-second pause. It does
  not pretend to be a browser and it does not solve bot checks.
- An article that could not be reached is tried again on later runs, three
  attempts in all.

## What it cannot do

- **It sees only the outlets in `feeds.txt`.**
- **Paywalls and refusals.** Where a page is a paywall teaser, a bot check or a
  refusal, only headline, byline and summary are checked. A quote deeper in the
  article is missed.
- **"Read in full" is an estimate.** The script measures how much text came back;
  it cannot prove nothing was cut.
- **Namesakes and spellings.** Matching is on exact names: a namesake is a false
  match, an unlisted spelling is a miss.
- **No history** from before the first run; feeds carry only recent items.
- **Feeds die and sites change.** Someone has to maintain `feeds.txt`.

What the starter list gave on one connection in Italy (audit of 6 October 2026,
952 articles, updated with the runs of the following day), for 34 outlets:

| How much is read | Outlets |
|---|---|
| Full article text | 21 |
| Some articles in full, many as teasers | 5 (La Stampa, la Repubblica, El País, Nikkei Asia, South China Morning Post) |
| Headline and summary only | 5 (Financial Times, The Economist, Le Monde, Balkan Insight, The Japan Times) |
| Nothing: the site refuses the script | 3 (Rapporteur, Il Post, East Asia Forum) |

Run `--audit` on your own connection; results depend on where the requests come from.

## Tests

```
python3 -m unittest discover -s tests -v
```

58 tests, standard library only. None touches the internet: each starts small
web servers on the computer that play the part of news sites and of a model
server. The ones the design rests on:

- `test_no_request_contains_a_name` records every request made during a run, a
  feed check, a diagnosis and an audit, and fails if any contains part of a name.
- `test_what_is_requested_does_not_depend_on_the_list` runs the monitor with three
  different lists and fails unless the requests are identical.
- `test_report_loads_nothing_from_the_internet` fails if the report references
  any outside resource other than links to the articles.
- `test_model_on_another_computer_is_refused_before_anything_is_sent` fails if a
  run with a non-local model address makes any request at all.
- `test_news_sites_still_receive_no_names_when_the_step_is_on` repeats the first
  check with the model step switched on.

`.github/workflows/tests.yml` runs the suite on Linux, Windows and macOS.

## Status

- Tested on Linux and macOS (the suite) and run on macOS against live sites.
  **Not yet run on Windows** outside the automated tests.
- The language-model step has been run with Qwen3 14B and Gemma 4 12B through
  Ollama on a Mac with 24 GB of memory. On 24 invented cases in six languages
  (`evaluate_models.py`) both gave 21 correct answers, and neither marked the
  listed person as a namesake. On real matches, Qwen3 14B often answered "likely
  the listed person" for people who did not fit the description it was given. Treat its verdicts as hints.
- Written with an AI assistant (Claude) and not yet reviewed by a developer.
- Whether automated reading is allowed by each outlet's terms of use has not
  been checked outlet by outlet.

## Proposals, not built

- **An institutional model for the namesake step.** The step works today with a
  model on the user's own computer. Pointing it at a model on the institution's
  own infrastructure would give every user the same model without installing one.
- **One central instance.** Running the monitor once a day on an institutional
  server would send all requests from one address at a steady pace, instead of
  several colleagues' computers asking the same sites. It moves the lists of
  names from laptops to that server, which is a data-protection decision.

## Licence

Copyright (c) 2026 Juliana Tomazini

Licensed under the EUPL

European Union Public Licence v. 1.2 (`EUPL-1.2`). The full text is in `LICENSE`.
Anyone who distributes this program, changed or not, or offers it to others as
an online service, must do so under the same licence and make the source code
available.
