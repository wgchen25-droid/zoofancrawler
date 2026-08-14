# Expanded Zoo List Blocker — RESOLVED / SUPERSEDED

**Status:** **RESOLVED** on 2026-08-14; this report is **SUPERSEDED** as an
active blocker.

**Historical role:** retain the pre-list search evidence below; do not treat
the former “missing authoritative list” conclusion as current.

## Resolution

The previously missing membership input is now available at the user-supplied
[`data/zoofan_phase0_zoo_list_v0_1.md`](../data/zoofan_phase0_zoo_list_v0_1.md),
supplied/identified on **2026-08-14**. It is **Phase0 v0.1**, with complete-file
SHA-256
`6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`.

The authoritative file parses to **73 raw rows and 73 unique rows/names**.
The final [`config/zoos.yaml`](../config/zoos.yaml) contains **73 unique stable
IDs**, one matching provenance entry per row, and all 73 mapped source lines;
the deterministic merge dropped **zero** rows. Exact country, group, tier,
gap, source, and plan counts are in
[`zoo-list-summary.md`](zoo-list-summary.md).

The four no-site rows remain visible with no fabricated URL or source:

- `mini-zoo-venray` — Mini ZOO Venray
- `kleindierwandelpark-hedel` — Kleindierwandelpark Hedel
- `dierenpark-de-bontekoe` — Dierenpark De Bontekoe
- `mini-animal-park-deventer` — Mini animal park Deventer

These are roster/source gaps, not missing roster members. The historical
blocker is therefore closed. This resolution records configuration and dated
source-research evidence only; it does **not** claim a live crawl, browser
acceptance, or final project completion.

## What the superseded audit concluded

Before the user-supplied file was available, the repository audit concluded
that it could prove only the then-current three-entry registry and could not
recover the requested expanded membership or taxonomy. That conclusion was
appropriate for the evidence available at that time and is preserved here for
traceability. It is superseded by the authoritative file above, not silently
deleted or rewritten as if the earlier search had found it.

The historical audit's merge rule was stable `id`, then `slug`, then the
normalized official URL. It found the same three identities in the then-current
config, Git snapshots, and SQLite projections; those repeated projections were
not additive membership. The new Phase 0 import uses the roster's deterministic
stable-ID and research-matching contract documented in
[`source-research/import-contract.md`](source-research/import-contract.md).

## Historical search coverage and result

The pre-list audit searched:

- tracked, untracked, ignored, and hidden workspace files;
- all reachable Git commits and refs, including checkpoint-like refs;
- unreachable Git text blobs and trees;
- YAML, JSON, Markdown, report, and artifact content; and
- every SQLite database and `.db` path in `data/` and `artifacts/`.

Its search terms covered three categories:

- approval/version markers: `approved`, `final`, `phase-0`, `phase 0`,
  `master`, `latest`, `checkpoint`;
- membership/list markers: `zoo list`, `zoo-list`, `zoo_list`, `zoos`,
  `roughly 50`, `about 50`, `50 zoos`; and
- grouping markers: `group`, `groups`, `taxonomy`, `tier`, `region`,
  `country`, `phase`.

The historical result was that no expanded roster or taxonomy was recoverable
from those surfaces. Both repository commits (`f7aab81` and `ec65921`) then
contained the same three-zoo configuration. Broad hits such as `latest`,
`group`, and `master` were ordinary code, generated output, or Git sample text,
not roster authority.

The exact structured/report paths inspected then included:

- `config/zoos.yaml`
- `README.md`, `PROJECT.md`, and `AGENTS.md`
- `artifacts/endgoal-report.json`
- Markdown, YAML, and JSON files under the repository, including `reports/`
  and `artifacts/`
- `artifacts/acceptance/acceptance-live.db`
- `artifacts/acceptance/endgoal-acceptance.db`
- `artifacts/remediation/final-live.db`
- `data/acceptance.db`
- `data/zoofan.db`

Exactly four inspected files were SQLite databases with a `zoos` table, and
each repeated the same three IDs:

- `artifacts/acceptance/acceptance-live.db`
- `artifacts/acceptance/endgoal-acceptance.db`
- `artifacts/remediation/final-live.db`
- `data/acceptance.db`

`data/zoofan.db` was a zero-byte placeholder with no schema and therefore no
membership evidence. The 12 physical rows across the four projections were
copies of one three-member projection, not 12 roster members. This historical
search evidence remains useful context, but it no longer blocks the supplied
73-row import.

## Current boundary

The active authority and current report are
[`data/zoofan_phase0_zoo_list_v0_1.md`](../data/zoofan_phase0_zoo_list_v0_1.md),
[`config/zoos.yaml`](../config/zoos.yaml), and
[`zoo-list-summary.md`](zoo-list-summary.md). Source research is linked from
the summary. Any later runtime crawl or dashboard acceptance is a separate
evidence layer and must not be inferred from this resolved blocker report.
