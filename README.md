make endgoal

# ZooFanCrawler

ZooFanCrawler is a small, polite multi-source article crawler for the three
configured zoos. The `make endgoal` command is the single end-to-end acceptance
entry point. It runs the complete test suite, creates a fresh
`data/acceptance.db`, performs two real `crawl all` runs from
`config/zoos.yaml`, checks official-domain/article validity and SQL
deduplication, runs failure isolation, then smoke-tests the dashboard in a real
Playwright browser. It makes a best-effort attempt to write the durable report;
the dashboard screenshot is produced only after the dashboard smoke reaches
its home page successfully.

For the full project and architecture guide, see [PROJECT.md](PROJECT.md). For
agent development conventions, see [AGENTS.md](AGENTS.md).

## Setup

Use Python 3.9+ and install the declared dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

The first browser acceptance run may install Chromium with the current Python
environment when Playwright has no executable. The crawl is intentionally live:
network access and the configured official sites are required for a PASS.

## Local dashboard and control services

After installing dependencies, start the local inspection Dashboard and its
control service together from any directory:

```sh
./start.sh
```

The launcher uses `.venv/bin/python` when available (or `python3`; set
`PYTHON_BIN` to select another interpreter), keeps both services on
`127.0.0.1`, and opens the control page unless `ZOOFAN_NO_OPEN=1` is set. The
two local URLs are `http://127.0.0.1:8000` (Dashboard) and
`http://127.0.0.1:8001` (control). Change them without editing files:

```sh
DASHBOARD_PORT=8100 CONTROL_PORT=8101 ./start.sh
```

Both services use the same persistent `data/zoofan.db`; the launcher creates
the `data/` directory but never deletes or replaces that database. It only
starts the two web services: it does not crawl, schedule a crawl, or create
background crawler work. Press `Ctrl-C` to stop both services. Any in-memory
control state is intentionally lost when the control service restarts; SQLite
data remains in `data/zoofan.db`.

## Commands

The CLI keeps the normal crawl aliases alongside the acceptance command:

```sh
python3 cli.py crawl all --db data/zoofan.db
python3 cli.py crawl berlin --db data/zoofan.db
python3 cli.py crawl koeln --db data/zoofan.db
python3 cli.py crawl gaia --db data/zoofan.db
python3 cli.py scheduler --db data/zoofan.db --once
python3 cli.py dashboard --db data/zoofan.db --host 127.0.0.1 --port 8000
make endgoal
```

The scheduler's default interval is six hours. The dashboard is a read-only
Flask view of the selected SQLite database and exposes `/`, `/articles`,
`/sources`, and `/runs`.

## Configuration and database

`config/zoos.yaml` is the source registry. It currently contains Zoo Berlin,
Kölner Zoo, and GaiaZOO, with RSS, sitemap, server-rendered archive, and
registry/health-check source records. Crawler records live in SQLite tables for
zoos, sources, articles, article discoveries, crawl runs, and per-source run
stats. Article identity uses canonical URL, normalized URL, and content hash;
the acceptance workflow additionally checks raw HTML/hash presence and the
official-domain plus configured allow policy.

To add a fourth zoo, add an enabled `zoos` entry with a stable `id`, `slug`,
`name`, official `website_url`, and at least one `sources` entry. Configure the
source's `kind`, official host, and article allow/exclude rules narrowly, then
run a normal single-zoo crawl before running `make endgoal`. The acceptance
gate currently requires exactly the three configured zoos, so adding a fourth
requires an intentional acceptance-policy update.

## Source findings and crawler limits

RSS/Atom feeds are parsed as XML; sitemap indexes and URL sets are traversed
with configured limits; archive sources use configured CSS selectors and are
server-rendered (SSR) HTML. Playwright is used for dashboard browser smoke,
not as a hidden crawler fallback. The URL policy rejects homepage, ticket,
shop, restaurant, contact, privacy/terms/cookie, jobs, generic events/calendar,
and navigation links unless a source's explicit article allow rule authorizes a
path. Source status and errors remain visible in the dashboard and report.

The current architecture is deliberately small and adapter-shaped: config and
domain records feed discovery, fetching, parsing, crawler orchestration, and
SQLite storage; the dashboard reads the resulting tables. Scaling the registry
to roughly 50 zoos is a known risk: endpoint rate limits and changing CMS
markup, pagination volume, source-specific policy maintenance, serial network
latency, SQLite write contention, and browser acceptance duration all grow with
the registry. Technical debt includes generic parser selectors, per-site
allow-list upkeep, and the lack of a production queue/observability backend.

This project does not promise JavaScript-rendered source discovery, bypass
robots/rate limits, or guarantee article availability when an official site is
down. Those are scope limits, not reasons to substitute fixtures or mocks for
the acceptance crawl.

## Acceptance artifacts

`make endgoal` removes only the explicitly named `data/acceptance.db`, creates a
fresh schema, and preserves the following artifacts:

- `artifacts/endgoal-report.json`: status, timestamps, per-zoo source and
  article counts/latest article, run-1/run-2 new canonical counts, errors,
  source findings, test/failure-isolation/dedup/dashboard gate evidence.
- `artifacts/dashboard-acceptance.png`: the rendered dashboard overview
  captured by the real Playwright smoke.

Normal local databases and Python caches are ignored by `.gitignore`; the
required root-level acceptance artifacts remain visible.
