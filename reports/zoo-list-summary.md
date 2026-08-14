# Phase 0 73-zoo roster summary

**Audit date:** 2026-08-14

**Scope:** authoritative roster, final configuration, source-research evidence,
fresh two-round live result, and static acceptance browser smoke summarized
below. Historical root artifacts are not current evidence.

## Authority and result

The authoritative membership input is
[`data/zoofan_phase0_zoo_list_v0_1.md`](../data/zoofan_phase0_zoo_list_v0_1.md),
the user-supplied **Phase0 v0.1** snapshot. Its complete-file SHA-256 is
`6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`.
The snapshot was supplied/identified on **2026-08-14** and is the membership
authority; research artifacts provide evidence and source plans but do not add
members.

The final registry is
[`config/zoos.yaml`](../config/zoos.yaml). It contains **73 raw zoo rows and
73 unique stable IDs**. Every config row has one matching provenance record
for the authoritative path, version, SHA, and `expanded_count: 73`. Every one
of the 73 roster rows is represented in the final config; no roster row was
dropped.

The applicable research evidence is kept under
[`reports/source-research/`](source-research/), including the deterministic
[`import-contract.md`](source-research/import-contract.md),
[`core-germany.json`](source-research/core-germany.json),
[`core-europe.json`](source-research/core-europe.json),
[`core-global.json`](source-research/core-global.json),
[`regional-nrw.json`](source-research/regional-nrw.json), and
[`regional-dutch.json`](source-research/regional-dutch.json), plus their
adapter-plan JSON files. Each scope records the same roster SHA.

## Fresh Phase0 live result

The equivalent fresh temporary two-round crawl covered all 73 enabled rows in
both rounds:

| Round | Run ID | Zoo coverage | Articles | Discovery relations | Inserted | Updated |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | `1f0f213ae0d34af9836c3a6fe0e20d3b` | 73/73 | 31 | 34 | 31 | — |
| 2 | `c9735cb5d87f4a5f918e2ce1f4530c47` | 73/73 | 31 | 34 | 0 | 0 |

The final public classifications are `SUPPORTED` 25,
`SUPPORTED_NO_CURRENT_NEWS` 12, `ROBOTS_DISALLOWED` 19,
`SOURCE_NOT_FOUND` 11, `UNSUPPORTED` 4, `BLOCKED` 2, and zero for
`JAVASCRIPT_REQUIRED`, `PARTIALLY_SUPPORTED`, and `FAILED`. There were 13
source errors (12 robots decisions plus one ZSL HTTP 403) and eight site
failures; all are external limitations. No parser, configuration, storage,
runtime, or quality blocker was recorded. The preserved Zoo Berlin/Kölner Zoo
regression records remain stored; GaiaZOO is represented by external
no-current-items evidence.

These classifications describe what the crawler could safely establish. They
do not turn a robots-disallowed, blocked, unsupported, or no-source site into
a successful article crawl, and no browser/authentication/robots bypass was
used. The run is equivalent fresh live evidence, not a claim that `make
endgoal` regenerated the tracked root artifacts.

Static acceptance browser smoke generation
`c49257ece8394a83ae072175f9c05483` confirmed 73 actual DOM rows, one result
for `Zoo Berlin` search, 25 `SUPPORTED` rows, working detail behavior and safe
links, with no `raw_html`, overflow, console errors, or page errors. The final
offline test evidence is **588 passed, 0 skipped** under
`PYTHONWARNINGS=error`.

## Parsing, normalization, and deduplication

The deterministic audit used these rules:

- Parse the five roster tables in file order. Trim whitespace around each cell
  only; preserve Unicode names, accents, apostrophes, and blank city cells.
- The five disjoint roster partitions are `Germany` 10, `Europe` 10,
  `Global` 10, `NRW` 25, and `Dutch Border` 18, giving `10 + 10 + 10 + 25 +
  18 = 73` raw rows.
- For a new stable ID, apply Unicode NFKD normalization, transliterate to
  ASCII where possible, lowercase, remove punctuation, join words with
  hyphens, and collapse repeated hyphens. The existing aliases are preserved:
  `zoo-berlin`, `koeln-zoo`, and `gaiazoo` remain the stable IDs for the three
  legacy entries. An ID is never derived from a website or host.
- Merge research by declared stable ID when it verifies against the roster
  name; otherwise match the exact roster name within its declared partition
  after the stable-ID normalization. A duplicate, missing match, or conflicting
  membership field is an import error, not a reason to guess.
- The 73 trimmed roster rows have 73 unique full rows and 73 unique zoo names.
  The 73 config `source_line` values are unique and match every roster row
  (Markdown lines 15 through 92, including section headers between tables).
  Config names, city values, country codes, tiers, and groups all match their
  mapped roster row. Thus normalization removed **zero** rows.
- The final config also has 73 unique slugs and 69 unique normalized official
  URLs; the remaining four rows intentionally have no official URL, documented
  below. No normalized URL collision was found.

## Roster counts

### Planning partitions and group overlap

| Roster partition | Rows | Exact groups assigned to every row |
| --- | ---: | --- |
| Core / Germany | 10 | `core30`, `germany_top10` |
| Core / Europe | 10 | `core30`, `europe_top10` |
| Core / Global | 10 | `core30`, `global_top10` |
| Regional Lab / NRW | 25 | `regional_lab`, `nrw` |
| Regional Lab / Dutch Border | 18 | `regional_lab`, `dutch_border` |

The exact group counts are: `core30` **30**, `regional_lab` **43**,
`germany_top10` **10**, `europe_top10` **10**, `global_top10` **10**, `nrw`
**25**, and `dutch_border` **18**. These memberships intentionally overlap:
each row has exactly two groups, for **146 group memberships** across 73 rows.
The broad groups (`core30` and `regional_lab`) overlap their corresponding
subgroup by design; their counts must not be added as if they were disjoint.
The five planning partitions themselves are disjoint and sum to 73.

`Region` is a planning bucket, not an inferred administrative-region field.
`NRW` is normalized to `North Rhine-Westphalia` only where the config's
administrative `region` field requires it; `Europe`, `Global`, and `Dutch
Border` remain planning labels. Group membership is taken from the roster
heading/contract, never inferred from country, city, or source availability.

### Country

Country counts below are derived from the roster names and checked against the
final config's ISO country codes:

| Country | ISO code | Rows |
| --- | --- | ---: |
| Germany | `DE` | 35 |
| Netherlands | `NL` | 20 |
| United States | `US` | 3 |
| United Kingdom | `GB` | 2 |
| Austria | `AT` | 1 |
| Australia | `AU` | 1 |
| Belgium | `BE` | 1 |
| Brazil | `BR` | 1 |
| Canada | `CA` | 1 |
| Czechia | `CZ` | 1 |
| Denmark | `DK` | 1 |
| France | `FR` | 1 |
| Japan | `JP` | 1 |
| Singapore | `SG` | 1 |
| South Africa | `ZA` | 1 |
| Switzerland | `CH` | 1 |
| Taiwan | `TW` | 1 |

The country counts are mutually exclusive and sum to 73. They are not
inferred from domains or names.

### Monitoring tier

| Tier | Rows |
| --- | ---: |
| `Core` | 30 |
| `Priority` | 12 |
| `Secondary` | 22 |
| `Directory` | 9 |

Tiers are draft planning labels from the authoritative snapshot. They are
mutually exclusive and sum to 73; they do not imply source readiness.

## Preserved legacy identities and regression source preservation

The three pre-existing identities were preserved exactly while the Phase0 rows
were imported. Their regression evidence remains stored. Source IDs are stable
source identities, not fresh IDs derived from the roster:

| Stable ID | Name | Official URL | Preserved source IDs and kinds |
| --- | --- | --- | --- |
| `zoo-berlin` | Zoo Berlin | `https://www.zoo-berlin.de/` | `zoo-berlin-news` (`archive`), `zoo-berlin-press` (`registry_only`) |
| `koeln-zoo` | Kölner Zoo | `https://koelnerzoo.de/` | `koeln-zoo-news-sitemap` (`sitemap`), `koeln-zoo-news-archive` (`registry_only`) |
| `gaiazoo` | GaiaZOO | `https://www.gaiazoo.nl/` | `gaiazoo-news-rss` (`rss`), `gaiazoo-news-archive` (`archive`), `gaiazoo-sitemap` (`registry_only`), `gaiazoo-press` (`registry_only`) |

Their exact preserved source URLs are:

- `zoo-berlin-news` → `https://www.zoo-berlin.de/de/aktuelles/news`;
  `zoo-berlin-press` → `https://www.zoo-berlin.de/de/aktuelles/presse`.
- `koeln-zoo-news-sitemap` →
  `https://koelnerzoo.de/wp-sitemap-posts-aktuellartical-1.xml`;
  `koeln-zoo-news-archive` → `https://koelnerzoo.de/aktuell/`.
- `gaiazoo-news-rss` → `https://www.gaiazoo.nl/category/nieuws/feed/`;
  `gaiazoo-news-archive` → `https://www.gaiazoo.nl/nieuws-vlogs/`;
  `gaiazoo-sitemap` → `https://www.gaiazoo.nl/sitemap_index.xml`;
  `gaiazoo-press` → `https://www.gaiazoo.nl/pers/`.

The legacy source rows intentionally have no per-source `source_status` field
in the final YAML; the zoo-level static statuses are `configured`,
`configured`, and `success`, respectively. This is configuration evidence, not
a claim that any of these sources currently crawls successfully.

## Official-site and executable-source gaps

### Four missing official websites

The following four authoritative rows have no verified official website. Their
`website_url` is intentionally omitted, `source_status` is `not_found`, and
their `sources` list is empty. No directory, social, municipal, or search-result
URL was promoted to an official source:

| Stable ID | Roster name | Tier | Country | Config evidence |
| --- | --- | --- | --- | --- |
| `mini-zoo-venray` | Mini ZOO Venray | Directory | `NL` | `website_url: null`, `sources: []` |
| `kleindierwandelpark-hedel` | Kleindierwandelpark Hedel | Directory | `NL` | `website_url: null`, `sources: []` |
| `dierenpark-de-bontekoe` | Dierenpark De Bontekoe | Directory | `NL` | `website_url: null`, `sources: []` |
| `mini-animal-park-deventer` | Mini animal park Deventer | Directory | `NL` | `website_url: null`, `sources: []` |

These are **4 missing official-site rows**, and also **4 rows with no
configured source row**. They remain in the 73-row roster.

### News-source coverage (all rows versus enabled checks)

The words “source present” and “source executable” are not interchangeable in
this repository:

| Static config measure | Count | Meaning |
| --- | ---: | --- |
| Zoo rows with any configured source row | 69 | The four no-site rows above have none. |
| Zoo rows with at least one enabled article-capable kind | 31 | At least one enabled `archive`, `rss`, `sitemap`, `json_api`, `atom`, or `custom_adapter` declaration exists; this does not override a blocked/unsupported status. |
| Enabled source checks with an article-capable kind | 44 | The executable portion of the 63 enabled checks; this is not a live-readiness count. |
| Enabled `registry_only` health checks | 19 | Registration/health checks that deliberately produce no article candidates. |
| Enabled source checks in total | 63 | 44 article-capable plus 19 `registry_only`; 53 configured rows remain disabled or explicit unsupported. |
| Configured source rows in total | 116 | Includes disabled, health/registry, and explicitly unsupported rows. |
| Configured source rows with a non-executable kind | 43 | `registry_only` 22 plus `unsupported` 21. |

The 29 rows with no article-capable kind are grouped by their static zoo
`source_status` here, so the gap is auditable without treating a blocked source
as a successful one:

| Static zoo status | Rows without an enabled article-capable kind | Stable IDs |
| --- | ---: | --- |
| `javascript_required` | 2 | `wilhelma`, `erlebnis-zoo-hannover` |
| `robots_disallowed` | 9 | `tierpark-hagenbeck`, `zoo-duisburg`, `zoo-krefeld`, `zoo-dortmund`, `aachener-tierpark-euregiozoo`, `terrazoo-rheinberg`, `heimat-tierpark-olderdissen`, `safariland-stukenbrock`, `wildwald-vosswinkel` |
| `blocked` | 2 | `gruener-zoo-wuppertal`, `dierenpark-t-goor` |
| `unsupported` | 4 | `allwetterzoo-muenster`, `tiergarten-kleve`, `tiergarten-moenchengladbach`, `naturwildpark-granat` |
| `source_not_found` | 7 | `sea-life-oberhausen`, `tiergehege-kaisergarten`, `affen-und-vogelpark-eckenhagen`, `wildpark-grafenberg`, `biotopwildpark-anholter-schweiz`, `hochwildpark-rheinland`, `zoo-im-brueckenkopfpark` |
| `partial` | 1 | `bird-park-de-lorkeershoeve` |
| `not_found` | 4 | `mini-zoo-venray`, `kleindierwandelpark-hedel`, `dierenpark-de-bontekoe`, `mini-animal-park-deventer` |

The 116 configured source rows have these kinds:

| Kind | Configured rows | Enabled checks |
| --- | ---: | ---: |
| `archive` | 40 | 28 |
| `rss` | 14 | 8 |
| `sitemap` | 14 | 8 |
| `json_api` | 5 | 0 |
| `registry_only` | 22 | 19 |
| `unsupported` | 21 | 0 |
| **Total** | **116** | **63** |

The source-row status field is absent on 8 preserved legacy rows and otherwise
has these static values: `success` 42, `partial` 18, `robots_disallowed` 19,
`unsupported` 9, `source_not_found` 7, `javascript_required` 6, `blocked` 5,
`parse_error` 1, and `success_no_items` 1. These values describe configured
evidence and intended handling; they are not a later run result.

## Research and adapter-plan status

The source-research reports use a different vocabulary from `Zoo.source_status`.
Research status records what was observed during the dated investigation;
adapter-plan status records whether a bounded source shape was proposed. A
research `verified_live`/`success` row can therefore have a config source that
is `registry_only`, `unsupported`, `partial`, or `javascript_required`. Neither
layer is a live acceptance result.

| Scope | Research evidence | Adapter/config-plan evidence |
| --- | --- | --- |
| Core / Germany (10) | `core-germany.json`: `success` 7, `javascript_required` 2, `robots_disallowed` 1; official sites and news/press/blog pages found for 10/10. | `core-germany-adapters.json`: executable archive/sitemap plans 10; registry-only plans 10. |
| Core / Europe (10) | `core-europe.json`: official websites verified 10/10 and news/archive sources verified 10/10; this artifact has no row-level aggregate status field. | `core-europe-adapters.json`: 29 source plans, 23 enabled plans, 12 ready archive plans, 6 fallback sitemap plans, 6 registry/health plans, and 6 blocked/disabled plans. These plan counters are plan-level evidence and may overlap by role. |
| Core / Global (10) | `core-global.json`: `success` 2, `partial` 4, `blocked` 4. | `core-global-adapters.json`: row status `supported` 2, `partial` 4, `robots_disallowed` 4; 20 source plans in total. |
| Regional Lab / NRW (25) | `regional-nrw.json`: `success` 20, `partial` 4, `blocked` 1. | `regional-nrw-adapters.json`: `ready` 5, `registry_only` 15, `unsupported` 5; 20 gap rows under that plan vocabulary. |
| Regional Lab / Dutch Border (18) | `regional-dutch.json`: official zoo sites verified 14, official news/feed sources verified 13, and 5 explicit identity/source gaps. | `regional-dutch-adapters.json`: `ready` 2, `ready_stale` 4, `ready_filtered` 1, `ready_http_only` 1, `ready_archive_rss_unverified` 1, `unsupported` 3, `registry_only` 4, `source_not_found` 2. |

The five explicit Dutch research gaps are `mini-zoo-venray`,
`dierenpark-t-goor`, `kleindierwandelpark-hedel`, `dierenpark-de-bontekoe`, and
`mini-animal-park-deventer`. Four have no verified official site;
`dierenpark-t-goor` has an official site identity but no executable source
under the dated access/robots evidence. This research-gap list is distinct from
the 29-row kind-based config gap above.

The static final-config zoo `source_status` distribution across all 73 rows is:

| Config `source_status` | Rows |
| --- | ---: |
| `success` | 27 |
| `robots_disallowed` | 13 |
| `partial` | 11 |
| `source_not_found` | 7 |
| `not_found` | 4 |
| `unsupported` | 4 |
| `blocked` | 3 |
| `configured` | 2 |
| `javascript_required` | 2 |

These are configuration/research disposition counts. They must not be
relabelled as `SUPPORTED`, `FAILED`, or any other live acceptance category.
All 73 config rows have `enabled: true`; enablement preserves roster coverage
and does not assert that a source is executable or currently reachable.

## Verification boundary

The count evidence was checked with deterministic scripts that (1) hash and
parse the Markdown tables, (2) load `config/zoos.yaml`, (3) map every config
provenance `source_line` to exactly one roster row, (4) compare every mapped
name/city/country/tier/group field, and (5) count IDs, slugs, normalized URLs,
groups, tiers, countries, source kinds, and statuses. The assertions were:

```text
roster raw rows = 73; unique rows/names = 73
config rows = 73; unique IDs/slugs = 73/73
provenance rows/unique source lines = 73/73; mismatches = 0; dropped rows = 0
group memberships = 146 (exactly two per roster row)
official URLs = 69; normalized URL collisions = 0; missing official URLs = 4
configured source rows = 116; enabled article-capable-kind zoo rows = 31
enabled source checks = 63; enabled article-capable checks = 44; enabled registry_only checks = 19
configured article-capable source rows = 73; non-executable source rows = 43
```

This document records the resolved roster input and the static research/config
snapshot. A future live crawl or dashboard acceptance run is a separate
evidence layer and must be reported separately; no such success is asserted
here. The historical missing-list search report is now explicitly marked
resolved/superseded in [`zoo-list-blocker.md`](zoo-list-blocker.md).
