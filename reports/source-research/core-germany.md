# Core Germany source research

**Checked:** 2026-08-14
**Scope:** the ten Germany rows under `Core 30` in [`data/zoofan_phase0_zoo_list_v0_1.md`](../../data/zoofan_phase0_zoo_list_v0_1.md)
**Roster SHA256:** `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`

## Decision

All ten official websites were reachable. Seven zoos have at least one
robots-readable, server-rendered source suitable for an `archive` or `sitemap`
proposal. Zoo Frankfurt's safer archive plan passed two final bounded
remediation retests and is now verified and recommended. Wilhelma and
Erlebnis-Zoo Hannover have an evidenced
JavaScript/client-rendering constraint. Tierpark Hagenbeck has reachable source
pages but no valid `robots.txt`; its source is therefore fail-closed until a
valid robots document is available. The Zoo Berlin Panda-Blog and Kölner Zoo
REST endpoint are retained as discovery evidence only: neither has a bounded
executable plan in this report.

| Stable ID | Exact roster name | City | Groups | Tier | Official site | Source status | JS evidenced | Recommended source proposal(s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `zoo-berlin` | Zoo Berlin | Berlin | `core30`, `germany_top10` | Core | [zoo-berlin.de](https://www.zoo-berlin.de/) | `success` / `SUPPORTED` | no | [news archive](https://www.zoo-berlin.de/de/aktuelles/news); press is `registry_only`; [Panda-Blog](https://www.zoo-berlin.de/de/aktuelles/panda-blog) is discovery evidence only |
| `tierpark-berlin` | Tierpark Berlin | Berlin | `core30`, `germany_top10` | Core | [tierpark-berlin.de](https://www.tierpark-berlin.de/) | `success` / `SUPPORTED` | no | [news archive](https://www.tierpark-berlin.de/de/aktuelles/news); press is `registry_only` |
| `zoo-leipzig` | Zoo Leipzig | Leipzig | `core30`, `germany_top10` | Core | [zoo-leipzig.de](https://www.zoo-leipzig.de/) | `success` / `SUPPORTED` | no | [news archive](https://www.zoo-leipzig.de/aktuelles/neuigkeiten/), [news sitemap](https://www.zoo-leipzig.de/type/news/sitemap.xml) |
| `koeln-zoo` | Kölner Zoo | Cologne | `core30`, `germany_top10` | Core | [koelnerzoo.de](https://koelnerzoo.de/) | `success` / `SUPPORTED` | no | [post-type sitemap](https://koelnerzoo.de/wp-sitemap-posts-aktuellartical-1.xml); [REST API](https://koelnerzoo.de/wp-json/wp/v2/aktuellartical) is discovery evidence only |
| `zoo-frankfurt` | Zoo Frankfurt | Frankfurt | `core30`, `germany_top10` | Core | [zoo-frankfurt.de](https://www.zoo-frankfurt.de/) | `success` / `SUPPORTED` | no | [press/news archive](https://www.zoo-frankfurt.de/de/presse/aktuell); verified safe plan |
| `wilhelma` | Wilhelma | Stuttgart | `core30`, `germany_top10` | Core | [wilhelma.de](https://www.wilhelma.de/) | `javascript_required` / `JAVASCRIPT_REQUIRED` | yes | [combined News & Presse](https://www.wilhelma.de/aktuelles/aktuelles/news-presse) (registry-only evidence; unsupported under JS constraint) |
| `tierpark-hellabrunn` | Tierpark Hellabrunn | Munich | `core30`, `germany_top10` | Core | [hellabrunn.de](https://www.hellabrunn.de/) | `success` / `SUPPORTED` | no | [news archive](https://www.hellabrunn.de/der-tierpark/aktuelles/neues-aus-hellabrunn), [press archive](https://www.hellabrunn.de/presse) |
| `erlebnis-zoo-hannover` | Erlebnis-Zoo Hannover | Hannover | `core30`, `germany_top10` | Core | [zoo-hannover.de](https://www.zoo-hannover.de/) | `javascript_required` / `JAVASCRIPT_REQUIRED` | yes | [news](https://www.zoo-hannover.de/news), [press](https://www.zoo-hannover.de/pressemitteilungen) (registry-only evidence; unsupported with no approved custom adapter) |
| `tiergarten-nuernberg` | Tiergarten Nürnberg | Nuremberg | `core30`, `germany_top10` | Core | [tiergarten.nuernberg.de](https://tiergarten.nuernberg.de/) | `success` / `SUPPORTED` | no | [current news](https://tiergarten.nuernberg.de/entdecken/aktuell), [press archive](https://tiergarten.nuernberg.de/presse) |
| `tierpark-hagenbeck` | Tierpark Hagenbeck | Hamburg | `core30`, `germany_top10` | Core | [hagenbeck.de](https://www.hagenbeck.de/) | `robots_disallowed` / `ROBOTS_DISALLOWED` | no | News and press pages retained as `registry_only` evidence (blocked by invalid robots endpoint) |

The `groups` values are inferred only from the roster headings and note:
`Core 30` → `core30`, and its Germany 10 subdivision → `germany_top10`.
No zoo association, geography, language, or institutional affiliation was
invented as an additional group; `cross_memberships` is empty in the JSON.

### Reviewed stable-ID alias

The reviewed configured ID for Tiergarten Nürnberg is
`tiergarten-nuernberg`. The mechanically transliterated candidate
`tiergarten-nurnberg` is recorded only as an alias warning; it must not be
introduced as a second zoo ID or source-ID prefix.

## Counts and gaps

| Metric | Count |
| --- | ---: |
| Roster rows / unique stable IDs | 10 / 10 |
| Official websites reachable | 10 |
| Official news, press, or blog page found | 10 |
| `SUPPORTED` | 7 |
| `JAVASCRIPT_REQUIRED` | 2 |
| `ROBOTS_DISALLOWED` | 1 |
| `SOURCE_NOT_FOUND` | 0 |
| Source records (including alternates, registry-only pages, and discovery evidence) | 24 |
| Crawl-ready without extra runtime constraint | 7 |
| Recommended source records | 10 |

The two JavaScript findings are evidence-based, not a general assumption that
modern sites use scripts: Wilhelma's page explicitly says to activate
JavaScript, while Hannover's Next.js news/press pages expose the listing data
through client/RSC payloads but no current article anchors in the no-JS HTML.
Hagenbeck's news and press HTML is static and parseable, but crawling must not
start because `https://www.hagenbeck.de/robots.txt` resolves to the site's HTML
404 page instead of a robots document.

Kölner Zoo's official WordPress post-type sitemap is the recommended source.
The REST API is directly observed discovery evidence, but has no bounded JSON
adapter plan in this report and is not recommended for executable import. The
broad [`/feed/`](https://koelnerzoo.de/feed/) endpoint is reachable but contains generic site pages; the official
[`category/neuigkeiten/feed/`](https://koelnerzoo.de/category/neuigkeiten/feed/)
feed was empty at check time, so both are recorded as a non-primary partial
feed rather than silently promoted.

Zoo Frankfurt's `zoo-frankfurt-press-news` source is verified and recommended
after two final bounded live retests. Each run discovered 20 candidates,
fetched and parsed one clean article, and produced zero inserts, updates, or
errors. The remediation evidence recorded HTTP 200, candidate
`/de/presse/news-detail/farewell...`, exact `div.article` markup, same-site
canonical `/de/news/news-detail/...`, and visible date `14.07.2026`. The
verified plan uses `article_content_selector: div.article` and anchors
candidates to
`^https://www\.zoo-frankfurt\.de/de/(?:presse|news)/news-detail/[^/?#]+/?$`.
Run IDs are `ec1df691fae04461a894a34823a8c0a8` and
`709e9d1d2a4f49829cf1a82112e2da7f`; evidence remains at
`/tmp/zoofan-phase0.9Jx9xK`. Raw-only variation was excluded and business
`updated_at` remained stable.

Config status semantics are intentionally separate from these raw research
observations: `source_status` in the config is safe operational readiness,
while each raw `source_status`/`research_source_status` in this report is
evidence metadata. A directly reachable source can therefore remain
`discovery_only`, `registry_only`, or `unsupported` in the config when no
approved bounded adapter exists; that is not a contradiction.

## Source proposals

The proposals use the repository's existing adapter vocabulary. `archive`
means server-rendered HTML list parsing, `sitemap` means an official XML URL
set, `json_api` means a bounded official JSON endpoint, `custom_adapter` means
the page needs a site-specific contract, and `registry_only` records an
official information page without claiming article discovery.

| Zoo | Role | URL | Kind / adapter proposal | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| Zoo Berlin | news | [source](https://www.zoo-berlin.de/de/aktuelles/news) | `archive` / `archive` | success | `/de/aktuelles/news/artikel/` links and pagination |
| Zoo Berlin | press | [source](https://www.zoo-berlin.de/de/aktuelles/presse) | `registry_only` / `registry_only` | success | Official press page retained as health evidence; no executable selectors or pagination plan |
| Zoo Berlin | blog | [source](https://www.zoo-berlin.de/de/aktuelles/panda-blog) | `archive` / discovery-only | success | Official Panda-Blog evidence; no bounded executable plan is declared |
| Tierpark Berlin | news | [source](https://www.tierpark-berlin.de/de/aktuelles/news) | `archive` / `archive` | success | `/de/aktuelles/news/artikel/` links and pagination |
| Tierpark Berlin | press | [source](https://www.tierpark-berlin.de/de/aktuelles/presse) | `registry_only` / `registry_only` | success | Official press page retained as health evidence; no stable article-list contract |
| Zoo Leipzig | news | [archive](https://www.zoo-leipzig.de/aktuelles/neuigkeiten/) | `archive` / `archive` | success | Server-rendered TYPO3 `/artikel/` links |
| Zoo Leipzig | news | [sitemap](https://www.zoo-leipzig.de/type/news/sitemap.xml) | `sitemap` / `sitemap` | success | Official news sitemap child |
| Zoo Leipzig | press | [source](https://www.zoo-leipzig.de/presse/) | `registry_only` / `registry_only` | success | Press factsheets/downloads; no current press list contract evidenced |
| Kölner Zoo | news | [sitemap](https://koelnerzoo.de/wp-sitemap-posts-aktuellartical-1.xml) | `sitemap` / `sitemap` | success | Official `aktuellartical` post type |
| Kölner Zoo | news | [REST API](https://koelnerzoo.de/wp-json/wp/v2/aktuellartical) | `json_api` / discovery-only | success | Official endpoint evidence; no bounded JSON field, URL-policy, pagination, or max-page plan is declared |
| Kölner Zoo | news | [archive](https://koelnerzoo.de/aktuell/) | `registry_only` / `registry_only` | success | Bricks dynamic popup cards; health evidence only because no stable article hrefs are in raw HTML |
| Kölner Zoo | press | [source](https://koelnerzoo.de/presse/) | `registry_only` / `registry_only` | success | Press contacts, media kit, accreditation, image downloads |
| Kölner Zoo | news | [RSS](https://koelnerzoo.de/feed/) | `rss` / `rss` | partial | Broad feed; category feed was empty |
| Zoo Frankfurt | press/news | [archive](https://www.zoo-frankfurt.de/de/presse/aktuell) | `archive` / `archive` | success | Keep the archive selectors and robots evidence; verified plan adds `div.article` content extraction and `/de/(presse|news)/news-detail/<slug>` allow; 20 candidates; retests `ec1df691fae04461a894a34823a8c0a8`, `709e9d1d2a4f49829cf1a82112e2da7f` |
| Wilhelma | news/press | [source](https://www.wilhelma.de/aktuelles/aktuelles/news-presse) | `registry_only` / `registry_only` | javascript_required | Explicit JS notice; unsupported without browser/runtime approval |
| Tierpark Hellabrunn | news | [archive](https://www.hellabrunn.de/der-tierpark/aktuelles/neues-aus-hellabrunn) | `archive` / `archive` | success | `/newsdetailseite/` links and pagination |
| Tierpark Hellabrunn | press | [archive](https://www.hellabrunn.de/presse) | `archive` / `archive` | success | Press releases and `/newsdetailseite-1/` links |
| Erlebnis-Zoo Hannover | news | [source](https://www.zoo-hannover.de/news) | `registry_only` / `registry_only` | javascript_required | Next.js/RSC client listing; unsupported, with no approved custom adapter name |
| Erlebnis-Zoo Hannover | press | [source](https://www.zoo-hannover.de/pressemitteilungen) | `registry_only` / `registry_only` | javascript_required | Next.js/RSC listing and PDF data; unsupported, with no approved custom adapter name |
| Erlebnis-Zoo Hannover | blog | [source](https://www.zoo-hannover.de/blog) | `registry_only` / `registry_only` | javascript_required | No stable `/blog/...` links in raw response; unsupported, with no approved custom adapter name |
| Tiergarten Nürnberg | news | [archive](https://tiergarten.nuernberg.de/entdecken/aktuell) | `archive` / `archive` | success | Current list, detail links, year archives |
| Tiergarten Nürnberg | press | [archive](https://tiergarten.nuernberg.de/presse) | `archive` / `archive` | success | Press list, detail links, year archives |
| Tierpark Hagenbeck | news | [source](https://www.hagenbeck.de/de/tierpark/tierpark/news_aktuelles.php) | `registry_only` / `registry_only` | robots_disallowed | Static `/de/_News/` links observed, but invalid robots blocks execution fail-closed |
| Tierpark Hagenbeck | press | [source](https://www.hagenbeck.de/de/pressebereich/pressemitteilungen.php) | `registry_only` / `registry_only` | robots_disallowed | Static `/de/pressebereich/pm/` links observed, but invalid robots blocks execution fail-closed |

## Robots evidence

| Zoo | Robots URL | Direct result |
| --- | --- | --- |
| Zoo Berlin | [robots.txt](https://www.zoo-berlin.de/robots.txt) | HTTP 200; only `/stiftung-rebrush/` disallowed |
| Tierpark Berlin | [robots.txt](https://www.tierpark-berlin.de/robots.txt) | HTTP 200; only `/stiftung-rebrush/` disallowed |
| Zoo Leipzig | [robots.txt](https://www.zoo-leipzig.de/robots.txt) | HTTP 200; current pages and news sitemap allowed |
| Kölner Zoo | [robots.txt](https://koelnerzoo.de/robots.txt) | HTTP 200; admin/generated upload paths only |
| Zoo Frankfurt | [robots.txt](https://www.zoo-frankfurt.de/robots.txt) | HTTP 200; empty `Disallow` |
| Wilhelma | [robots.txt](https://www.wilhelma.de/robots.txt) | HTTP 200; only `/typo3/` disallowed |
| Tierpark Hellabrunn | [robots.txt](https://www.hellabrunn.de/robots.txt) | HTTP 200; current pages and sitemap allowed |
| Erlebnis-Zoo Hannover | [robots.txt](https://www.zoo-hannover.de/robots.txt) | HTTP 200; `Allow: /` and sitemap declared |
| Tiergarten Nürnberg | [robots.txt](https://tiergarten.nuernberg.de/robots.txt) | HTTP 200; only TYPO3/admin paths disallowed |
| Tierpark Hagenbeck | [robots.txt](https://www.hagenbeck.de/robots.txt) | Redirects to HTML `/404.php`; invalid/unavailable, fail closed |

The machine-readable details, per-source HTTP/final URLs, evidence URLs, and
the same `checked_at` value are in
[`core-germany.json`](core-germany.json).
