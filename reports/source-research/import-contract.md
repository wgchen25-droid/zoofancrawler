# Phase 0 73-zoo import contract and final registry evidence

Status: the deterministic merge/import contract has been applied to the final
configuration. This document remains the validation boundary for provenance,
identity, source safety, and future roster revisions; it does not contact a
website or perform a crawl.

## 1. Authority and snapshot

`data/zoofan_phase0_zoo_list_v0_1.md` is the only membership authority. Its
complete-file SHA-256 for this snapshot is:

~~~text
6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd
~~~

The five sections are disjoint and contain 10 Germany, 10 Europe, 10 Global,
25 NRW, and 18 Dutch Border rows. The import derives the membership count by
parsing those rows and checking the partition sum; `73` is a snapshot
assertion, not a hard-coded crawler/reporting total.

The research artifacts corroborate identity, official endpoints, source
proposals, and limitations. They never add a zoo that is absent from the
authoritative markdown and never change its name, city, country, group, or
tier. A row that cannot be researched remains a configured roster row with
its gap and limitation recorded.

The final `config/zoos.yaml` contains all 73 authoritative rows, including the
three preserved legacy identities (`zoo-berlin`, `koeln-zoo`, `gaiazoo`) and the
four explicit no-site rows. Every row carries the same Phase0 provenance and
the exact two-group membership required by the snapshot. Reports may treat the
registry as the Phase0 membership boundary; research files still provide
evidence and limitations rather than additional membership.

The fresh two-round live evidence covered 73/73 rows in both runs, produced 31
articles and 34 discovery relations, and inserted/updated 0/0 on the second
run. Its public classifications are `SUPPORTED` 25,
`SUPPORTED_NO_CURRENT_NEWS` 12, `ROBOTS_DISALLOWED` 19,
`SOURCE_NOT_FOUND` 11, `UNSUPPORTED` 4, `BLOCKED` 2, with
`JAVASCRIPT_REQUIRED`, `PARTIALLY_SUPPORTED`, and `FAILED` all zero. Thirteen
source errors (12 robots plus one ZSL HTTP 403) and eight site failures are
external limitations; no parser/config/storage/runtime/quality blocker was
recorded.

## 2. Input artifacts and schema normalization

All paths below are local repository evidence. The importer reads them; it
does not fetch a URL named in an evidence field.

| Artifact | Declared shape | Membership role | Normalization note |
| --- | --- | --- | --- |
| `data/zoofan_phase0_zoo_list_v0_1.md` | Markdown table | Sole authority | `Region`, `Country`, `Zoo`, `City`, and `Tier` are the canonical row fields. |
| `reports/source-research/core-germany.json` | `source-research.v1`, `zoos` | Research for 10 Germany rows | Uses `id`/`slug`/`name`; map `id` to the canonical stable ID after roster verification. |
| `reports/source-research/core-europe.json` | Report record, `records` | Research for 10 Europe rows | Has no `schema_version`; use its `report_id`, field shape, and roster hash as recorded evidence. |
| `reports/source-research/core-global.json` | `core-global-source-research.v1`, `rows` | Research for 10 Global rows | Uses `stable_id`/`roster_name`; proposal kind `api` means `json_api`. |
| `reports/source-research/regional-nrw.json` | `regional-source-research.v1`, `rows` | Research for 25 NRW rows | Uses `stable_id`/`roster_name`; keeps row-level `source_status`, `js`, and evidence. |
| `reports/source-research/regional-dutch.json` | `regional-source-research/v1`, `entries` | Research for 18 Dutch Border rows | Uses `stable_id`/`roster_name`; retains blank cities and four rows with no official website. |
| `reports/source-research/core-europe-adapters.json` | `adapter-plan.v1`, `zoos` with nested `plans` | Adapter plans for the 10 Europe rows | `plan_status`, verification, robots, and bounded configs refine source activation; they do not add rows. |
| `reports/source-research/core-germany-adapters.json` | `source-adapter-plan.v1` | Execution plan for Germany only | Not a membership source. Its official-sites-only, fail-closed, no-browser policy and source plans may constrain the Germany import, but its candidate counts do not create rows. |
| `reports/source-research/core-global-adapters.json` | `core-global-adapters.v1`, `rows` with nested `sources` | Adapter plans for the 10 Global rows | `row_status`/source `source_status`, robots, runtime, and selectors are execution evidence; they do not replace the research row identity. |
| `reports/source-research/regional-dutch-adapters.json` | `regional-source-adapters.v1`, `rows` | Adapter plans for the 18 Dutch Border rows | `plan_status`, primary/fallback sources, and direct observations refine source activation; they do not add rows. |
| `reports/source-research/regional-nrw-adapters.json` | `regional-source-adapters.v1`, `plans` | Adapter plans for the 25 NRW rows | `source_status` here is often a plan disposition (`ready`, `registry_only`, `unsupported`); use the underlying NRW research status for the zoo status. |

The matching order is deterministic:

1. Parse the authoritative roster in file order and normalize only whitespace
   around cell values. Keep Unicode names and accents exactly as written.
2. Match one research record by its declared stable ID when that ID verifies
   against the roster name; otherwise match by exact roster name within the
   declared partition after applying the stable-ID normalization below.
3. Require every research record to match exactly one roster row and every
   roster row to have at most one merged identity. A duplicate, missing
   partition row, or conflicting country/city/group/tier is an import error,
   not a reason to guess.
4. Merge evidence fields from all matching artifacts. On a conflict, the
   authoritative roster wins for membership fields; research conflicts remain
   visible under `metadata.source_research.conflicts` and block automatic
   source activation until reviewed.

The accompanying `*.md` research reports are explanatory evidence only. They
may explain a JSON gap but cannot add a source or membership that the JSON and
roster do not establish.

## 3. Canonical zoo identity

### Stable IDs and slugs

For a new row, derive the ID from the exact roster `Zoo` value:

1. Unicode NFKD normalize.
2. Transliterate to ASCII where possible.
3. Lowercase, remove punctuation, join whitespace-separated words with
   hyphens, and collapse repeated hyphens.
4. Validate against the config identifier grammar.

The ID is independent of an official URL, host, redirect, search result, or
source path. The canonical `slug` equals the ID unless an existing stable
alias is explicitly preserved.

The three existing IDs are preserved as explicit aliases during the merge:

| Roster name | Canonical ID | Reason |
| --- | --- | --- |
| `Zoo Berlin` | `zoo-berlin` | Existing ID and deterministic derivation agree. |
| `Kölner Zoo` | `koeln-zoo` | Preserve the existing configured/storage identity; do not silently replace it with a new transliteration. |
| `GaiaZOO` | `gaiazoo` | Existing ID and deterministic derivation agree. |

The `koeln-zoo` alias must be kept in an import manifest or equivalent
metadata so the exception is reviewable. Do not derive IDs from a domain,
URL spelling, English translation, or a later rebrand. A changed website
must not rename a zoo.

### Reviewed Germany and NRW umlaut/transliteration aliases

The following reviewed spellings are identity evidence, not extra roster rows
or additional IDs. Preserve the configured ID in the middle column exactly;
retain the other spelling only in `metadata.id_aliases_reviewed` when present.
In particular, the Nürnberg spelling is a reviewed `-nuernberg` ID, not the
mechanical `-nurnberg` candidate.

| Roster name | Reviewed configured ID | Reviewed alias spelling | Treatment |
| --- | --- | --- | --- |
| `Kölner Zoo` | `koeln-zoo` | existing transliteration exception | Preserve the existing storage/source identity; do not replace it with a newly derived spelling. |
| `Tiergarten Nürnberg` | `tiergarten-nuernberg` | `tiergarten-nurnberg` | Keep `tiergarten-nuernberg`; the mechanical candidate is review-only and must not create a duplicate. |
| `Grüner Zoo Wuppertal` | `gruener-zoo-wuppertal` | `grüner-zoo-wuppertal` | Keep the ASCII configured ID; Unicode spelling is an alias only. |
| `Allwetterzoo Münster` | `allwetterzoo-muenster` | `allwetterzoo-münster` | Keep the ASCII configured ID; Unicode spelling is an alias only. |
| `Aquazoo Löbbecke Museum` | `aquazoo-loebbecke-museum` | `aquazoo-löbbecke-museum` | Keep the ASCII configured ID; Unicode spelling is an alias only. |
| `Tiergarten Mönchengladbach` | `tiergarten-moenchengladbach` | `tiergarten-mönchengladbach` | Keep the ASCII configured ID; Unicode spelling is an alias only. |
| `Zoo im Brückenkopfpark` | `zoo-im-brueckenkopfpark` | `zoo-im-brückenkopfpark` | Keep the ASCII configured ID; Unicode spelling is an alias only. |

Alias metadata is diagnostic and must not weaken uniqueness checks: canonical
IDs and normalized official website URLs remain globally unique.

Source IDs follow the same stability rule. Preserve every existing source ID
for the three legacy zoos. For a new source, use its explicit research ID;
otherwise derive `<zoo-id>-<role>` from a normalized role/name. If two roles
collide, append a 1-based ordinal in normalized-URL order. Never use a random
UUID or a URL query string as identity.

The final merge is ordered by roster order, then by source research order. A
review or serializer that needs sorted output may sort by canonical ID, but
must not change identity.

### Canonical fields

The existing `Zoo` model already has the fields needed for the registry:
`id`, `slug`, `name`, `website_url`, `country_code`, `language`, `enabled`,
`metadata`, `groups`, `region`, `city`, `source_status`, and
`list_provenance`. Use those fields rather than introducing parallel keys.
The model has no first-class tier field, so tier belongs in `metadata` as
specified below.

`name`, `city`, `country_code`, and tier source from the roster. A research
`official_name` or verified city is retained as evidence and does not
silently replace the roster value.

Enablement is operational, not membership. Keep all 73 rows in the configured
zoo registry. Set a zoo and its approved official `registry_only` health source
enabled when an official URL exists, even if the research status is blocked,
robots-limited, JavaScript-required, or otherwise a known external limitation;
the normal fetcher must then fail closed and retain that limitation. Set a zoo
disabled only when it has no official site/source (the four no-site rows) or
only an explicit `unsupported` plan. A no-site row has no fabricated source;
its `sources` list is empty and its static status is `not_found`. An
`unsupported` source is disabled. A custom source is enabled only after its
approved registry adapter is available. This keeps roster coverage visible
without turning evidence gaps into executable guesses.

## 4. Exact group and phase mapping

Every expanded row receives exactly the group set below, in the shown order.
Group membership is an explicit taxonomy, not a classifier inferred from
geography or source availability.

| Roster partition | Exact groups | Rows |
| --- | --- | ---: |
| Core / Germany | `core30`, `germany_top10` | 10 |
| Core / Europe | `core30`, `europe_top10` | 10 |
| Core / Global | `core30`, `global_top10` | 10 |
| Regional Lab / NRW | `regional_lab`, `nrw` | 25 |
| Regional Lab / Dutch Border | `regional_lab`, `dutch_border` | 18 |

Derived membership invariants are therefore:

~~~text
core30 = 30
regional_lab = 43
germany_top10 = 10
europe_top10 = 10
global_top10 = 10
nrw = 25
dutch_border = 18
~~~

No row may have duplicate groups, `Ungrouped`, `unknown`, or a group invented
from a source report. The final 73-row registry gives every row the exact
memberships above; the preserved three legacy identities are ordinary Phase0
rows with their explicit group values.

### Phase bucket versus administrative region

The roster's `Region` column mixes planning buckets (`Germany`, `Europe`,
`Global`, and `Dutch Border`) with the explicit NRW abbreviation. Do not
overload `Zoo.region` with that planning taxonomy.

| Partition | `metadata.phase_bucket` | `metadata.phase_subgroup` | `Zoo.region` |
| --- | --- | --- | --- |
| Core / Germany | `core30` | `germany_top10` | Omit; `Germany` is a planning/country bucket, not a federal region. |
| Core / Europe | `core30` | `europe_top10` | Omit; `Europe` is a planning bucket. |
| Core / Global | `core30` | `global_top10` | Omit; `Global` is a planning bucket. |
| Regional Lab / NRW | `regional_lab` | `nrw` | `North Rhine-Westphalia`; this is a fixed normalization of the explicit `NRW` label, not a city inference. |
| Regional Lab / Dutch Border | `regional_lab` | `dutch_border` | Omit; `Dutch Border` is a cross-border planning bucket, not an administrative province. |

Retain the raw roster label as `metadata.roster_region_bucket`. Never infer a
German state, Dutch province, or other administrative region from city,
website, or country. If a future authoritative source supplies an
administrative region, it must be added explicitly and separately from the
phase fields.

Copy a non-empty roster city exactly. The three blank Dutch Border cities
(`Bird Park De Lorkeershoeve`, `Ten Kate's Dierenpark`, and `Dierenpark De
Bontekoe`) remain null/omitted; an empty string is not a city.

## 5. Tier, country, and language

Store the roster tier exactly in `metadata.tier` with this closed vocabulary:
`Core`, `Priority`, `Secondary`, `Directory`. The snapshot counts are Core
30, Priority 12, Secondary 22, and Directory 9. Tier affects planning only;
it never changes group membership, adapter kind, enabled status, or source
status.

Use the research ISO 3166-1 alpha-2 `country_code`, checked against
`SUPPORTED_COUNTRY_CODES` in `zoofan/config.py`. The import must derive the
country code from explicit research/roster evidence, never from a domain or
name. The snapshot country counts are `DE=35`, `NL=20`, `US=3`, `GB=2`, and
one each for `CZ`, `AT`, `BE`, `DK`, `CH`, `FR`, `CA`, `SG`, `JP`, `TW`, `AU`,
`BR`, and `ZA`.

Set `Zoo.language` to the explicit primary ISO 639-1/639-2 value from the
research record, checked against `SUPPORTED_LANGUAGES`. A source may have a
different explicit `Source.language`; preserve that per source. Keep
additional observed languages in research metadata, but never infer a
language from country, name, URL, or the language of a search result. A row
with no valid primary language is an import error (or must remain disabled
until an explicit value is supplied), not an inferred fallback.

## 6. Official identity, URLs, and provenance

`website_url` must be a verified official HTTP(S) URL from research and is
normalized with `normalize_url` before comparison and storage. A third-party
directory, social profile, municipal reference, or search-result URL may be
retained as evidence but cannot become `website_url` or a crawl source unless
the research explicitly establishes it as an official operator endpoint.

The four Dutch rows with no verified official website remain present with a
null/omitted `website_url`:

~~~text
mini-zoo-venray
kleindierwandelpark-hedel
dierenpark-de-bontekoe
mini-animal-park-deventer
~~~

They must not receive a fabricated homepage. Their research gap and
identity evidence are retained; they have no executable source until an
official endpoint is explicitly verified.

Every expanded `Zoo` carries one simple, matching provenance entry. The
following shape is the minimum contract and is intentionally compatible with
the current validator's scalar-only `list_provenance` values:

~~~yaml
list_provenance:
  - source: data/zoofan_phase0_zoo_list_v0_1.md
    version: Phase0 v0.1
    sha256: 6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd
    authority: user_designated_authoritative_membership_list
    expanded_count: 73
    checked_at: "2026-08-14"
~~~

`expanded_count` is optional evidence and, when present, must equal the count
derived from the imported roster. Reporting must derive configured and
expanded counts from the loaded records and validate any declared count; it
must never use a production constant for the total.

The provenance entry must be identical across all 73 rows: same local path,
Phase0 v0.1 version, real SHA-256, and authority. A mixed legacy/Phase0 list,
an URL in place of the local path, a weak hash, or a partial set of entries is
not authoritative evidence. Such a config remains `existing_registry_only`
or invalid for expanded reporting.

Retain research evidence in a namespaced metadata structure, for example:

~~~yaml
metadata:
  phase_bucket: core30
  phase_subgroup: germany_top10
  roster_region_bucket: Germany
  tier: Core
  source_research:
    - artifact: reports/source-research/core-germany.json
      schema_version: source-research.v1
      checked_at: "2026-08-14"
      research_source_status: success
      status_reason: null
      evidence_urls: []
      gaps: []
      adapter_proposal: {}
      js_required: false
~~~

For each row, preserve the applicable artifact path/schema/check date, raw
research status and reason/detail, evidence URLs, gaps, adapter proposal,
JavaScript/robots observations, and explicit alternate official hosts (for
example the Biotopwildpark host migration). For each configured `Source`, a
namespaced `config.research` block may retain the source artifact, raw status,
evidence URLs, and notes; adapters must ignore that block when executing.
Evidence is diagnostic, not authorization. Do not store raw HTML, cookies,
credentials, bearer tokens, API secrets, or arbitrary response bodies in
config or report metadata.

## 7. Source status mapping

`Zoo.source_status` and `Source.source_status` use the configuration
vocabulary from `SOURCE_STATUS_VOCABULARY`; this is the safe operational
readiness used by the crawler. Raw research `source_status` and
`research_source_status` remain in namespaced metadata as reachability and
site-constraint evidence, so the mapping is reversible. Adapter kind and
status are separate: `registry_only` can accompany a verified identity
(`success`) while still providing no article discovery. Consequently, a
research record may be HTTP-200/`success` while its config source is
`unsupported`, `registry_only`, or `javascript_required` when no bounded safe
adapter is approved; that ready-vs-unsupported pairing is intentional, not a
contradiction.

| Research value | Canonical config status | Interpretation |
| --- | --- | --- |
| `success`, `verified_live`, source-level `verified` | `success` | Explicit official identity/source verified. |
| Adapter-plan `supported`, `ready` | `success` | An explicit adapter plan is ready; retain the underlying row evidence as well. |
| `verified_live_stale`, `verified_live_stale_static`, `verified_live_stale_feed`, `verified_live_http_only_stale`, `partial` | `partial` | Evidence is reachable but stale, sparse, HTTP-only, or only partly usable. |
| Adapter-plan `ready_stale`, `ready_filtered`, `ready_http_only`, `ready_archive_rss_unverified` | `partial` | A plan exists but its evidence is stale, filtered, HTTP-only, or not fully verified. |
| `javascript_required` | `javascript_required` | Client rendering is required; do not add a browser bypass. |
| `robots_disallowed` | `robots_disallowed` | Robots permission is disallowed or cannot be established under fail-closed policy. |
| `blocked`, `blocked_direct_probe`, `advertised_but_probe_blocked`, `official_site_indexed_but_fetch_blocked_403` | `blocked` | Access challenge/403 is evidenced but is not specifically a robots decision. |
| `no_official_site_verified`, `municipal_identity_only_no_site`, `official_identity_ambiguous_no_site`, `municipal_label_only_no_site` | `not_found` | No official crawlable site/source was established. |
| `source_not_found` | `source_not_found` | The research explicitly found no source; retain that canonical distinction. |
| `unsupported` | `unsupported` | An explicit source is non-executable; do not use this for an unknown kind. |

When a blocked detail explicitly says robots permission is unavailable or
disallowed, use `robots_disallowed` rather than `blocked`. Do not classify a
plain HTTP status as a robots outcome without that evidence.

`core-europe.json` has no row-level `source_status`; map a row to `success`
only when its proposed official source has an explicit `status: verified` (as
the current ten rows do), otherwise use the status supported by the evidence.
The adapter-plan artifacts are not a second research-status authority. Their
config `source_status` expresses operational readiness, while the raw
research status stays in metadata. In particular,
`registry_only`, `fallback`, `fallback_only`, `health_only_no_news_detail_contract`,
`disabled_noncanonical_candidates`, and `discovery_only` describe a source
plan, not a `Zoo.source_status`. Use the underlying row research status when
one exists. If no row status exists, an explicitly verified official identity
with only a registry/health plan maps to `partial`, while a no-site row maps to
`not_found`. A plan `unsupported` source maps to `unsupported` at source level;
the zoo keeps its underlying identity status. `source_not_found` remains the
canonical source status. The adapter-plan JSON may therefore change source
activation to `registry_only`/`unsupported` without changing roster identity.

For a row with no explicit aggregate status, aggregate source statuses in this
order:

1. If any recommended executable source maps to `success`, use `success` and
   retain every other limitation per source.
2. Otherwise choose the first present status in
   `configuration_error`, `failed`, `robots_disallowed`, `blocked`,
   `javascript_required`, `partial`, `not_found`, `unsupported`.
3. If no source evidence exists, use `not_found` only when the research says
   no official identity/source was found; otherwise fail the import for
   missing evidence.

This static status is not a claim about a future crawl run. Runtime statuses
(`source_not_found`, `parse_error`, `timeout`, and so on) remain runtime
evidence and must not be erased by the import.

## 8. Adapter and source rules

Use the canonical kinds in `zoofan/adapters.py`. Research aliases are
normalized as follows: `api`, `json`, and `json_api` to `json_api`; `feed` to
`rss`; `archive`/`html`/`news_archive` to `archive`; and explicit health or
registry spellings to `registry_only`. An unknown token is an import/config
error, not silently `unsupported`.

### `archive`

Use only for an evidenced server-rendered HTML news/press/blog listing with a
narrow entry URL, selectors, article URL allow rule, and bounded pagination.
Keep press contact pages as evidence or `registry_only` unless they are an
actual article archive. The Germany adapter plan currently caps archive
pagination at two pages; retain such plan limits rather than inventing a
larger crawl.

### `rss` and `atom`

Use an official feed URL only when the research verifies it and its scope is
narrow enough to be news/article content. `atom` is a distinct source kind
when the document is explicitly Atom; a generic `feed` proposal canonicalizes
to `rss` and the parser may detect Atom content. A feed URL mentioned in a
page but not verified is evidence, not a source.

### `sitemap`

Use only an official sitemap endpoint that was verified or explicitly
proposed as a bounded fallback. Require a narrow allow rule for article URLs,
honor robots and official-host checks for every child sitemap and `loc`, and
bound sitemap traversal (the Germany adapter plan currently caps it at one
sitemap). An advertised or blocked sitemap is not an executable source.

### `json_api`

Map research `api` proposals to `json_api` only when the endpoint, response
field mapping, article URL policy, and official host are explicit. Do not
turn a guessed WordPress REST path, a generic API mention, or a blocked probe
into a source. JSON configuration must remain data-only and must not contain
credentials.

### `custom_adapter`

Use only when research documents a site-specific contract that cannot be
represented by a built-in adapter and an approved injected registry name
exists. The name must pass the safe custom-adapter validation. Configuration
must not load arbitrary module paths, execute code, or use a browser as a
hidden fallback. If a generic source works, use the generic source instead.

### `registry_only`

Use for an official identity/health endpoint that must remain visible but has
no evidenced article-list contract, including press-only pages, JavaScript
listing pages, and invalid-robots plans. It performs no article discovery;
selectors and guessed pagination are forbidden. An official URL may be
health-checked under normal robots/fetch policy. A no-site row has no source
URL at all and remains a roster-only record rather than receiving a fake
registry endpoint.

### `unsupported`

Use only for an explicit, reviewed non-executable outcome (for example an
adapter plan that is intentionally unavailable). Keep the zoo/source row and
reason visible, normally disabled for crawling, and never use `unsupported`
as a catch-all for an unknown kind or an unresearched row.

For multiple sources, configure only the recommended primary sources and
explicitly approved, official fallbacks. A fallback is not automatically
enabled merely because it appears in `evidence_urls`; source IDs, URL policy,
status, and research evidence must all be present. All source URLs are
normalized before comparison, and their hosts must equal the official host or
an explicitly declared alias. Robots remain fail-closed, redirects are
rechecked, and no cross-domain or browser fallback is permitted.

## 9. What must never be inferred

The importer and config worker must not infer:

- group membership, phase subgroup, tier, or expanded-roster inclusion from
  city, country, popularity, source availability, or a report heading;
- a stable ID or official identity from a URL, host, search result, redirect,
  translation, or punctuation preference;
- an official website, source endpoint, source selector, pagination rule,
  article pattern, API schema, or custom adapter from a URL suffix or a broad
  homepage link;
- country, language, administrative region, or city from a domain, name,
  neighboring geography, or a third-party directory;
- currentness, publication dates, article counts, or crawl success from an
  HTTP 200 alone;
- a robots permission from a sitemap advertisement, or a permitted crawl from
  a failed/unparseable robots response;
- a browser/JavaScript workaround, authentication bypass, CAPTCHA bypass, or
  third-party mirror when the official source is blocked or client-rendered;
- an article source from a press contact page, municipal page, social profile,
  directory, or evidence URL that was not approved as a source;
- `unsupported`, `registry_only`, or a research gap as a reason to delete the
  roster row or overwrite its authoritative identity.

## 10. Validation invariants

The config worker must run these checks without network access before writing
the import:

### Membership and identity

- The parsed roster has 73 rows derived as `10 + 10 + 10 + 25 + 18`, and the
  five research partitions have exactly those row counts.
- The union of research IDs is exactly the roster ID set; all IDs are unique,
  URL-safe, and stable under the derivation algorithm. The three existing IDs
  (`zoo-berlin`, `koeln-zoo`, `gaiazoo`) remain unchanged.
- Every expanded row has the same Phase0 path/version/SHA provenance; the SHA
  matches the complete local roster file.
- Roster name, country, city (including the three null cities), and tier are
  retained exactly. No row is silently dropped because it has no source.

### Taxonomy and metadata

- Group sets and counts match the mapping in section 4; no duplicate,
  `Ungrouped`, or inferred group is present.
- Tier values are closed and counts are Core 30, Priority 12, Secondary 22,
  Directory 9.
- `phase_bucket`, `phase_subgroup`, and raw `roster_region_bucket` are
  separate from `Zoo.region`; only the 25 explicit NRW rows have normalized
  `North Rhine-Westphalia` there.
- Country/language values match supported ISO vocabularies. Country counts
  match section 5; enabled rows have an explicit valid primary language.

### URL, source, and status safety

- A non-null `website_url` is absolute HTTP(S), normalized, and supported by
  official research. The four no-site rows remain null/omitted and are not
  assigned municipal or directory URLs.
- Every source belongs to a configured zoo, has a unique stable ID, uses a
  canonical adapter kind, and has a normalized URL on the official host or an
  explicit research-backed alias. Registry-only/no-site rows do not gain
  guessed endpoints.
- Archive, sitemap, and JSON settings are bounded and validate under the
  existing config validators. Custom adapters are safe registry names only;
  unknown kinds fail validation.
- Every configured status is in `SOURCE_STATUS_VOCABULARY`, while the raw
  research status/reason is retained. `blocked`, `robots_disallowed`,
  `javascript_required`, stale, and no-site limitations remain visible rather
  than being converted into generic success or deleted rows.
- No network, browser, robots fetch, SQLite write, raw HTML, credential, or
  generated report is needed for this validation.

### No-site validator rule (implemented)

`Zoo.website_url` remains optional in `zoofan/models.py`. The final
`_validate_raw_config` rule permits a null official URL only for an
evidence-backed Phase0 no-site row with `not_found`/`source_not_found` status,
the exact local provenance, and no executable source. It continues rejecting
null URLs for ordinary rows. Strict validation therefore covers all 73 rows
without a fabricated URL or a dropped no-site row. A model change is not
needed; the existing metadata and provenance fields carry the identity-only
evidence.

## 11. File ownership and review boundaries

| Owner | Files/subsystem | Responsibility |
| --- | --- | --- |
| Config/import worker | `config/zoos.yaml`, `zoofan/config.py`, and config tests such as `tests/test_config.py` | Maintain the completed 73-row merge, the narrow no-site validation policy, strict ID/provenance checks, and deterministic source declarations. |
| Reporting worker | `zoofan/reporting.py`, `tests/test_reporting.py` | Derive Phase0 scope/counts from configured evidence and preserve external-limit classifications across output formats. |
| Crawler/discovery worker | `zoofan/crawler.py`, `zoofan/discovery.py`, `zoofan/adapters.py`, related tests | Execute only approved canonical sources, preserve robots/cross-host/security limits, and maintain source-failure isolation. |
| Storage worker | `zoofan/storage.py`, storage tests/migrations | Persist the imported records without changing identity/uniqueness semantics. |

The config worker may read the source-research artifacts and this contract but
must not rewrite them. No worker should edit generated reports, acceptance
artifacts, or another worker's owned files as part of a roster revision. The
deterministic contract is validated offline; the separate fresh live evidence
and browser smoke are reported in the final completion report.
