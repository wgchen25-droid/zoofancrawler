# Core Germany adapter plans

Checked: 2026-08-14
Roster: `data/zoofan_phase0_zoo_list_v0_1.md`
Roster SHA256: `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`

This is a conservative translation of the verified official-site research into current-config-shaped source plans. The JSON is deterministic and contains the complete source objects, validation details, and evidence URLs.

## Execution policy

Only official hosts are proposed. Robots remains fail-closed, no browser/rendering bypass is proposed, and archive pagination is bounded to at most two pages. A source that is JavaScript-dependent or blocked by invalid robots evidence is represented as `kind: registry_only` with an explicit unsupported action. Hannover's records use no custom adapter name: they remain `registry_only`/`unsupported` with `javascript_required` evidence.

## Counts

| Measure | Count |
| --- | ---: |
| Germany roster rows | 10 |
| Preserved current zoo definitions | 2 |
| Preserved current source definitions | 4 |
| Other zoo plans | 8 |
| Other source plans | 16 |
| Total source plans represented | 20 |
| Executable archive/sitemap plans | 10 |
| Registry-only plans | 10 |
| Official source endpoints checked | 20 |
| HTTP 200 source endpoints | 20 |
| Discovery-evidence records (not executable plans) | 2 |
| Validated candidate count (executable sources) | 837 |

The 837 total is the sum of the final checked response counts: Zoo Berlin 9, Kölner sitemap 10 after its two preserved exclusions, Tierpark Berlin 9, Zoo Leipzig archive 10, Zoo Leipzig news sitemap 709, Zoo Frankfurt 20, Hellabrunn 10 + 10, and Tiergarten Nürnberg 25 + 25. Registry-only entries have `candidate_count: null` when no stable listing contract was evidenced; JS entries have zero usable no-JS candidates; Hagenbeck was not executed because robots permission was invalid.

`zoo-berlin-panda-blog` and `koeln-zoo-news-api` are listed under
`discovery_evidence` in the JSON because their official endpoints were
observed. They are deliberately absent from `plans`, have
`recommended: false`, and do not contribute to the 20 source-plan count: no
bounded selectors/JSON fields, URL policy, pagination, or `max_pages` plan is
approved for either source. The preserved Zoo Berlin press definition remains
exactly `registry_only`.

## Preserved current definitions

The `source_definitions_from_config` objects in the JSON are exact semantic copies of the existing `config/zoos.yaml` source definitions for `zoo-berlin` and `koeln-zoo`; no selectors, source kinds, URLs, or config fields are changed.

| Source | Kind | URL | Validation |
| --- | --- | --- | ---: |
| `zoo-berlin-news` | `archive` | <https://www.zoo-berlin.de/de/aktuelles/news> | 9 |
| `zoo-berlin-press` | `registry_only` | <https://www.zoo-berlin.de/de/aktuelles/presse> | n/a |
| `koeln-zoo-news-sitemap` | `sitemap` | <https://koelnerzoo.de/wp-sitemap-posts-aktuellartical-1.xml> | 10/12 |
| `koeln-zoo-news-archive` | `registry_only` | <https://koelnerzoo.de/aktuell/> | n/a |

For Kölner Zoo, the direct official sitemap returned 12 `<loc>` entries. The preserved allow rule plus `monatsubersicht` and `jahresubersicht` exclusions leave 10 candidates. The sitemap contract is `urlset/url/loc` (with optional `urlset/url/lastmod`).

## Executable plans for the other eight zoos

The exact JSON `config` blocks are authoritative for implementation. Archive entries below were checked by running their selectors against the linked official response and passing the same selector set to `parse_archive_page`; Zoo Frankfurt additionally passed two final bounded remediation retests.

| Zoo | Source ID | Kind | Official source | Candidate count | Limit |
| --- | --- | --- | --- | ---: | ---: |
| Tierpark Berlin | `tierpark-berlin-news` | `archive` | <https://www.tierpark-berlin.de/de/aktuelles/news> | 9 | `max_pages: 2` |
| Tierpark Berlin | `tierpark-berlin-press` | `registry_only` | <https://www.tierpark-berlin.de/de/aktuelles/presse> | n/a | health only |
| Zoo Leipzig | `zoo-leipzig-news` | `archive` | <https://www.zoo-leipzig.de/aktuelles/neuigkeiten/> | 10 | `max_pages: 1` |
| Zoo Leipzig | `zoo-leipzig-news-sitemap` | `sitemap` | <https://www.zoo-leipzig.de/type/news/sitemap.xml> | 709 | `max_sitemaps: 1` |
| Zoo Leipzig | `zoo-leipzig-press` | `registry_only` | <https://www.zoo-leipzig.de/presse/> | n/a | health only |
| Zoo Frankfurt | `zoo-frankfurt-press-news` | `archive` | <https://www.zoo-frankfurt.de/de/presse/aktuell> | 20 | `max_pages: 2` |
| Wilhelma | `wilhelma-news-press` | `registry_only` | <https://www.wilhelma.de/aktuelles/aktuelles/news-presse> | 0 no-JS | unsupported |
| Hellabrunn | `tierpark-hellabrunn-news` | `archive` | <https://www.hellabrunn.de/der-tierpark/aktuelles/neues-aus-hellabrunn> | 10 | `max_pages: 2` |
| Hellabrunn | `tierpark-hellabrunn-press` | `archive` | <https://www.hellabrunn.de/presse> | 10 | `max_pages: 2` |
| Erlebnis-Zoo Hannover | `erlebnis-zoo-hannover-news` | `registry_only` | <https://www.zoo-hannover.de/news> | 0 no-JS | unsupported |
| Erlebnis-Zoo Hannover | `erlebnis-zoo-hannover-press` | `registry_only` | <https://www.zoo-hannover.de/pressemitteilungen> | 0 no-JS | unsupported |
| Erlebnis-Zoo Hannover | `erlebnis-zoo-hannover-blog` | `registry_only` | <https://www.zoo-hannover.de/blog> | 0 no-JS | unsupported |
| Tiergarten Nürnberg | `tiergarten-nuernberg-news` | `archive` | <https://tiergarten.nuernberg.de/entdecken/aktuell> | 25 | `max_pages: 2` |
| Tiergarten Nürnberg | `tiergarten-nuernberg-press` | `archive` | <https://tiergarten.nuernberg.de/presse> | 25 | `max_pages: 2` |
| Tierpark Hagenbeck | `tierpark-hagenbeck-news` | `registry_only` | <https://www.hagenbeck.de/de/tierpark/tierpark/news_aktuelles.php> | n/a | unsupported |
| Tierpark Hagenbeck | `tierpark-hagenbeck-press` | `registry_only` | <https://www.hagenbeck.de/de/pressebereich/pressemitteilungen.php> | n/a | unsupported |

### Tierpark Berlin

`tierpark-berlin-news` uses official host `www.tierpark-berlin.de`, allow `^https://www\.tierpark-berlin\.de/de/aktuelles/news/artikel/[^/?#]+/?$`, and excludes the official press path. Selectors are `article.news-grid-item`, `a[href*="/de/aktuelles/news/artikel/"]`, `[itemprop="headline"]`, and `time[itemprop="datePublished"]`; pagination is `.page-navigation a[href*="tx_news_pi1"][href*="currentPage"]`.

The press URL is intentionally `registry_only`: dated entries were reachable, but no stable article href contract was evidenced. Its only executable config field is `official_host`.

Evidence: [news](https://www.tierpark-berlin.de/de/aktuelles/news), [press](https://www.tierpark-berlin.de/de/aktuelles/presse), [robots](https://www.tierpark-berlin.de/robots.txt).

### Zoo Leipzig

`zoo-leipzig-news` uses official host `www.zoo-leipzig.de`, allow `^https://www\.zoo-leipzig\.de/artikel/[^/?#]+/?$`, and excludes the listing root. Selectors are `div.large-teaser[role="listitem"]`, `a[href*="/artikel/"]`, `.large-teaser__title`, and `.large-teaser__text-wrapper > p`. The response exposes an AJAX `data-url` pagination shape rather than a verified ordinary href selector, so the plan is bounded to `max_pages: 1`.

The selected date text is numeric `DD.MM.YYYY`; the existing generic date parser does not produce `published_at` for those values. The raw selected date remains evidence, but a date-parser change is outside this report.

`zoo-leipzig-news-sitemap` uses the same narrow allow/exclude rules, `max_sitemaps: 1`, and the direct sitemap fields `urlset/url/loc` and `urlset/url/lastmod`. Its 709 official `<loc>` values all matched the `/artikel/` allow rule.

The press URL remains `registry_only`: it is official and reachable but no current press-release list contract was evidenced.

Evidence: [archive](https://www.zoo-leipzig.de/aktuelles/neuigkeiten/), [sitemap](https://www.zoo-leipzig.de/type/news/sitemap.xml), [press](https://www.zoo-leipzig.de/presse/), [robots](https://www.zoo-leipzig.de/robots.txt).

### Zoo Frankfurt

`zoo-frankfurt-press-news` retains official host `www.zoo-frankfurt.de`, the
archive discovery selectors (`div.card`,
`a[href*="/de/presse/news-detail/"]`, `.card-title`, `.tags`), archive
pagination, and `max_pages: 2`. The verified remediation plan adds
`article_content_selector: div.article` and the anchored allow rule
`^https://www\.zoo-frankfurt\.de/de/(?:presse|news)/news-detail/[^/?#]+/?$`;
this accepts only the two official detail sections while preserving the same
official host. Both final bounded retests discovered twenty candidates and
fetched/parsed one clean article with zero inserts, updates, or errors. The
selected date is numeric `DD.MM.YYYY`, so the current generic parser leaves
`published_at` empty; this remains an explicit data-quality note. Live
evidence: HTTP 200 candidate `/de/presse/news-detail/farewell...`, same-site
canonical `/de/news/news-detail/...`, visible `14.07.2026`, and exact
`div.article`, recorded at `/tmp/zoofan-phase0.9Jx9xK`. Retest runs:
`ec1df691fae04461a894a34823a8c0a8` and
`709e9d1d2a4f49829cf1a82112e2da7f`. Raw-only variation was excluded and
business `updated_at` remained stable.

Evidence: [official archive](https://www.zoo-frankfurt.de/de/presse/aktuell), [robots](https://www.zoo-frankfurt.de/robots.txt).

### Wilhelma

`wilhelma-news-press` is explicitly `kind: registry_only`, `adapter_proposal: registry_only`, and `execution: unsupported`. Its only proposed config field is `official_host: www.wilhelma.de`.

The no-JS official response contains an explicit JavaScript-required notice. The candidate count is therefore zero executable no-JS candidates. No browser, rendering, or hidden endpoint fallback is proposed.

Evidence: [official listing](https://www.wilhelma.de/aktuelles/aktuelles/news-presse), [robots](https://www.wilhelma.de/robots.txt).

### Tierpark Hellabrunn

`tierpark-hellabrunn-news` uses official host `www.hellabrunn.de`, allow `^https://www\.hellabrunn\.de/newsdetailseite/[^/?#]+/?$`, and the narrow documented non-article exclusions in the JSON. Selectors are `div.news-list__item`, `a.news-list__link[href*="/newsdetailseite/"]`, `h2.news-list__title`, and `time.news-list__date`; pagination is `a[href*="/der-tierpark/aktuelles/neues-aus-hellabrunn/seite-"]`, with `max_pages: 2`.

`tierpark-hellabrunn-press` uses the same card/title/date selectors, allow `^https://www\.hellabrunn\.de/newsdetailseite-1/[^/?#]+/?$`, its press-page exclusions, and pagination `a[href*="/presse/seite-"]`, with `max_pages: 2`.

Both pages returned ten cards, ten unique allowed links, and ten parsed German dates.

Evidence: [news](https://www.hellabrunn.de/der-tierpark/aktuelles/neues-aus-hellabrunn), [press](https://www.hellabrunn.de/presse), [robots](https://www.hellabrunn.de/robots.txt).

### Erlebnis-Zoo Hannover

The `news`, `pressemitteilungen`, and `blog` sources are all explicit `registry_only`/`unsupported` plans with only their official host configured. Their official Next.js/RSC responses expose headings and runtime payloads but no stable usable no-JS article anchors. Each has candidate count zero and no browser or rendering bypass is proposed. No custom adapter name is declared or approved; enabling discovery requires a separately reviewed safe contract.

Evidence: [news](https://www.zoo-hannover.de/news), [press](https://www.zoo-hannover.de/pressemitteilungen), [blog](https://www.zoo-hannover.de/blog), [robots](https://www.zoo-hannover.de/robots.txt).

### Tiergarten Nürnberg

The reviewed stable ID is `tiergarten-nuernberg`. `tiergarten-nurnberg` is a
mechanical ASCII spelling noted for review only, not a second zoo or source
prefix.

`tiergarten-nuernberg-news` uses official host `tiergarten.nuernberg.de`, allow `^https://tiergarten\.nuernberg\.de/entdecken/aktuell/detail/news/[^/?#]+/?$`, and the narrow year/event exclusions in the JSON. Selectors are `article.article.articletype-0.topnews`, `a[href*="/entdecken/aktuell/detail/news/"]`, `h3.LIST-ITEM`, and `time`; pagination is `div.news.news-list .pagination li.next a[href*="/entdecken/aktuell/seite/"]`, with `max_pages: 2`.

`tiergarten-nuernberg-press` uses the same card/title/date selectors, allow `^https://tiergarten\.nuernberg\.de/presse/detail/news/[^/?#]+/?$`, the same narrow exclusions, and pagination `div.news.news-list .pagination li.next a[href*="/presse/seite/"]`, with `max_pages: 2`.

Each source validated 25 cards, 25 unique allowed article links, and 25 parsed dates.

Evidence: [news](https://tiergarten.nuernberg.de/entdecken/aktuell), [press](https://tiergarten.nuernberg.de/presse), [robots](https://tiergarten.nuernberg.de/robots.txt).

### Tierpark Hagenbeck

Both official Hagenbeck pages are represented as `registry_only`/`unsupported` with only `official_host: www.hagenbeck.de`. Static cards were visible during diagnostic inspection, but no candidate execution is recorded.

`https://www.hagenbeck.de/robots.txt` returned HTTP 200 after redirecting to `https://www.hagenbeck.de/404.php`, which is HTML rather than a valid robots document. The fail-closed action is to leave both sources non-executable; there is no robots bypass and no archive selector proposal in the executable config.

Evidence: [news](https://www.hagenbeck.de/de/tierpark/tierpark/news_aktuelles.php), [press](https://www.hagenbeck.de/de/pressebereich/pressemitteilungen.php), [robots](https://www.hagenbeck.de/robots.txt).

## Gaps and handoff

The executable set is ready to translate into config without broadening hosts or selectors. Zoo Leipzig and Zoo Frankfurt have numeric German date text that the current generic parser does not turn into `published_at`; that is an explicit data-quality note, not a reason to add an unverified parser.

Wilhelma and Erlebnis-Zoo Hannover require an approved JavaScript-capable or documented endpoint adapter before discovery can be enabled. Tierpark Hagenbeck requires a valid robots document. Tierpark Berlin and Zoo Leipzig press pages remain health-only registry sources because no stable article-list contract was evidenced.

No config, crawler code, or tests were changed for this report.

## Status vocabulary boundary

The raw research `source_status` records reachability and observed site
constraints. Config `source_status` is the safe operational readiness used by
the crawler. Thus an HTTP-200 research record may correctly map to a disabled
`unsupported`, `registry_only`, or `javascript_required` config source when no
narrow approved adapter plan exists; research evidence and operational
readiness are intentionally not the same field.
