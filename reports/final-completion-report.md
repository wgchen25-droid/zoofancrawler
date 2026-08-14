# ZooFanCrawler Phase0 final completion report

**Audit date:** 2026-08-14
**Decision:** **PASS for the Phase0 v0.1 73-zoo registry and fresh live
two-round evidence; external site limitations are accepted classifications.**

This report supersedes the earlier three-zoo/451-test completion note. The
membership authority is
[`data/zoofan_phase0_zoo_list_v0_1.md`](../data/zoofan_phase0_zoo_list_v0_1.md),
whose complete-file SHA-256 is
`6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`.
The final runtime registry in [`config/zoos.yaml`](../config/zoos.yaml) has 73
enabled zoo rows and preserves every authoritative roster row.

## Final scope and static inventory

| Measure | Final value | Boundary |
| --- | ---: | --- |
| Enabled zoo rows | 73 | Every row has Phase0 provenance and exactly two explicit groups. |
| Configured source rows | 116 | Includes disabled, `registry_only`, and explicit `unsupported` declarations. |
| Enabled source checks | 63 | 44 article-capable checks plus 19 `registry_only` health checks. |
| Verified official URLs | 69 | Four authoritative rows intentionally have no verified site. |
| Site-specific adapters added | 0 | No bespoke adapter was introduced. |
| Generic article kinds reused | 3 | `rss`, `sitemap`, and `archive`. |
| Operational source shapes incl. health | 4 | The three generic kinds plus `registry_only`. |
| Canonical adapter taxonomy | 8 | `rss`, `atom`, `sitemap`, `archive`, `json_api`, `custom_adapter`, `registry_only`, `unsupported`. |

The four no-site rows are `mini-zoo-venray`, `kleindierwandelpark-hedel`,
`dierenpark-de-bontekoe`, and `mini-animal-park-deventer`. They remain in the
roster as `not_found` rows with no source URL; a directory, social profile,
municipal page, or search result is not an official crawl source. This is an
intentional completeness decision, not a dropped-row failure.

The static zoo-level `source_status` distribution, rechecked from the YAML
without network access, is:

| Status | Rows |
| --- | ---: |
| `success` | 27 |
| `partial` | 11 |
| `robots_disallowed` | 13 |
| `source_not_found` | 7 |
| `unsupported` | 4 |
| `not_found` | 4 |
| `blocked` | 3 |
| `configured` | 2 |
| `javascript_required` | 2 |

These are configuration/research dispositions, not live completion labels.

## Fresh live evidence

The fresh temporary database was crawled twice across all enabled Phase0 rows:

| Round | Run ID | Zoo results | Articles | Discovery relations | Inserted | Updated |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | `1f0f213ae0d34af9836c3a6fe0e20d3b` | 73/73 | 31 | 34 | 31 | — |
| 2 | `c9735cb5d87f4a5f918e2ce1f4530c47` | 73/73 | 31 | 34 | 0 | 0 |

The second round therefore added no article and changed no existing article.
The final zoo-level public classifications sum to all 73 rows:

| Public classification | Zoos | Meaning at this boundary |
| --- | ---: | --- |
| `SUPPORTED` | 25 | A bounded official source produced article support. |
| `SUPPORTED_NO_CURRENT_NEWS` | 12 | The source was usable but had no current items. |
| `ROBOTS_DISALLOWED` | 19 | Fail-closed robots policy prevented the request. |
| `SOURCE_NOT_FOUND` | 11 | No usable article source was established. |
| `UNSUPPORTED` | 4 | An explicit source shape is not executable. |
| `BLOCKED` | 2 | External access was blocked without a robots decision. |
| `JAVASCRIPT_REQUIRED` | 0 | No browser fallback is used for source discovery. |
| `PARTIALLY_SUPPORTED` | 0 | No partial result blocked the Phase0 decision. |
| `FAILED` | 0 | No parser/config/storage/runtime code failure was recorded. |

There were 13 source errors (12 robots decisions and one ZSL HTTP 403) and
eight site failures. All of these are external limitations, not code or data
quality blockers. The preserved Zoo Berlin and Kölner Zoo regression evidence
remains stored; GaiaZOO is represented by external no-current-items evidence.
No robots, authentication, CAPTCHA, or JavaScript workaround was used.

## Acceptance decision

| Gate | Decision | Evidence |
| --- | --- | --- |
| Phase0 membership | PASS | 73/73 authoritative rows imported; SHA/provenance and group checks agree. |
| Strict configuration | PASS | `validate-config --json` accepts the final roster, including the four explicit no-site rows. |
| Source registry | PASS | 116 declarations, 63 enabled checks, 44 article-capable + 19 health checks; no site-specific adapter added. |
| Two-round live coverage | PASS with external limitations | Both fresh runs have exactly 73/73 zoo results; all limitations are classified above. |
| Persistence and deduplication | PASS | 31 articles and 34 relations persist; round two inserted 0 and updated 0. |
| Failure isolation | PASS | Source/site failures remain per-source/per-zoo and do not prevent later rows from producing results. |
| Parser/config/storage/runtime/quality blockers | PASS | Zero blockers recorded in the fresh evidence. |
| Browser smoke | PASS | Static acceptance generation `c49257ece8394a83ae072175f9c05483`: actual DOM 73 rows, `Zoo Berlin` search 1, `SUPPORTED` filter 25, detail works, safe links present, and no `raw_html`, overflow, console errors, or page errors. |

“PASS with external limitations” means the crawler obeyed its safety and
isolation contract and reported the limitation honestly. It does not mean a
robots-disallowed, blocked, no-source, or unsupported site was reachable, nor
that missing articles were recovered.

The tracked `artifacts/endgoal-report.json` and
`artifacts/dashboard-acceptance.png` are historical snapshots. This evidence
was produced by an equivalent fresh temporary double crawl; it must not be
described as a `make endgoal` run or as regeneration of those root artifacts.

The final offline evidence is **588 passed, 0 skipped** under
`PYTHONWARNINGS=error`. Lint, typecheck, strict config, compile, and diff
checks remain separate gates.

## Reproduction commands

The strict, offline, live, report, and static-serving commands are:

```sh
# Strict config validation (no network, no SQLite writes).
python3 cli.py --config config/zoos.yaml validate-config --json

# Complete deterministic suite: 588 passed, 0 skipped.
PYTHONWARNINGS=error python3 -m pytest -q

# Offline aggregate gate; OFFLINE_DB must be a non-empty crawl DB.
make offline-gates \
  OFFLINE_DB=/path/to/acceptance.db \
  REPORT_DIR=/tmp/zoofancrawler-reports

# Fresh temporary two-round live crawl.
LIVE_DIR="$(mktemp -d /tmp/zoofancrawler-phase0-live.XXXXXX)"
LIVE_DB="$LIVE_DIR/acceptance.db"
python3 cli.py --config config/zoos.yaml crawl all --db "$LIVE_DB" | tee "$LIVE_DIR/run-1.json"
python3 cli.py --config config/zoos.yaml crawl all --db "$LIVE_DB" | tee "$LIVE_DIR/run-2.json"

# Build the five report views from that same DB.
python3 cli.py --config config/zoos.yaml build-acceptance-report \
  --db "$LIVE_DB" --output-dir reports

# Serve the generated static acceptance HTML (smoke generation:
# c49257ece8394a83ae072175f9c05483).
python3 -m http.server 8765 --directory reports/latest
```

The `make endgoal` command remains available for a deliberately networked
acceptance workflow, but it has destructive database/root-artifact behavior
and is not the provenance of the two run IDs above. Do not replace external
limitations with fixture success or infer current acceptance from the tracked
historical root artifacts.
