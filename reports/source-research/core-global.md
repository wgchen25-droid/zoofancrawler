# Core Global source research

Checked: 2026-08-14 (Europe/Berlin)  
Authority: user-confirmed `data/zoofan_phase0_zoo_list_v0_1.md`  
Roster SHA256: `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`

This report covers exactly the ten rows whose roster groups are `core30` and `global_top10`. Evidence is limited to official zoo, operator, municipal, government, or institutional sites and their first-party news, press, RSS, sitemap, and API endpoints. Search results and third-party directories were discovery hints only and are not evidence.

Status counts: 2 success, 4 partial, 4 blocked. A missing `robots.txt` (HTTP 404) is treated as blocked because the project Fetcher is fail-closed. JS/API observations describe the source as served; they do not authorize browser automation or a robots bypass.

## Roster and source summary

| Stable ID | Exact roster name | Country / city | Groups / tier | Official host | Proposed source | Language | JS | Status |
|---|---|---|---|---|---|---|---|---|
| `san-diego-zoo` | San Diego Zoo | US / San Diego | `core30`, `global_top10` / Core | `zoo.sandiegozoo.org` | Story Hub archive | en | no | success |
| `bronx-zoo` | Bronx Zoo | US / New York | `core30`, `global_top10` / Core | `bronxzoo.com` | WCS RSS + archive | en | no | success |
| `smithsonians-national-zoo` | Smithsonian's National Zoo | US / Washington, DC | `core30`, `global_top10` / Core | `nationalzoo.si.edu` | current news archive | en | no | partial |
| `toronto-zoo` | Toronto Zoo | CA / Toronto | `core30`, `global_top10` / Core | `www.torontozoo.com` | media-room press archive | en | no | blocked |
| `singapore-zoo` | Singapore Zoo | SG / Singapore | `core30`, `global_top10` / Core | `www.mandai.com` | filtered English sitemap | en | yes | partial |
| `ueno-zoo` | Ueno Zoo | JP / Tokyo | `core30`, `global_top10` / Core | `www.tokyo-zoo.net` | Japanese news archive | ja | no | blocked |
| `taipei-zoo` | Taipei Zoo | TW / Taipei | `core30`, `global_top10` / Core | `www.zoo.gov.taipei` | official RSS (JSON fallback) | zh (tag zh-Hant) | no | blocked |
| `taronga-zoo-sydney` | Taronga Zoo Sydney | AU / Sydney | `core30`, `global_top10` / Core | `www.taronga.org.au` | first-party article API (zero Sydney candidates) | en | yes | partial |
| `sao-paulo-zoo` | São Paulo Zoo | BR / São Paulo | `core30`, `global_top10` / Core | `www.zoologico.com.br` | first-party press API | pt (tag pt-BR) | yes | blocked |
| `national-zoological-garden` | National Zoological Garden | ZA / Pretoria | `core30`, `global_top10` / Core | `www.pretoriazoo.org` | WordPress RSS/API evidence (disabled) | en | no | partial |

## Row findings

### San Diego Zoo — `san-diego-zoo`

- Official site: [San Diego Zoo](https://zoo.sandiegozoo.org/); current operator/press host: [San Diego Zoo Wildlife Alliance](https://sandiegozoowildlifealliance.org/).
- Adapter: `archive`, [Story Hub search/news](https://sandiegozoowildlifealliance.org/story-hub/search-news). Keep article links on the Alliance's first-party story/press paths. [Media resources](https://sandiegozoowildlifealliance.org/story-hub/media-resources) is a secondary press-room page.
- Language `en`; JS not required; observed server-rendered HTML; status `success`. [robots.txt](https://sandiegozoowildlifealliance.org/robots.txt) does not disallow the proposed path.
- Caveat: the official [sitemap](https://sandiegozoowildlifealliance.org/sitemap.xml) contains stale `live-sdzwa.pantheonsite.io` locations, so it is not the proposed source.

### Bronx Zoo — `bronx-zoo`

- Official site: [Bronx Zoo](https://bronxzoo.com/); its first-party operator newsroom is [WCS Newsroom](https://newsroom.wcs.org/).
- Adapter: `rss`, [WCS news-release RSS](https://newsroom.wcs.org/DesktopModules/DnnForge%20-%20NewsArticles/Rss.aspx?TabID=13614&ModuleID=28242&MaxCount=25), with [news-release archive](https://newsroom.wcs.org/News-Releases.aspx) as a fallback. The feed is operator-wide (25 observed RSS items), not Bronx-specific; retain only the eight entries whose text passed the `Bronx Zoo`/`Bronx, NY` identity filter.
- Language `en`; JS not required; observed server-rendered WCS/DNN RSS. The bounded diagnostic fetched an official article with HTTP 200, found the exact `.article-body` content root, and found no `main` or `article` root. The source-scoped `article_content_selector: .article-body` passed the live retest: run `d1dc9a0cfc15474dbcb322d9e73b97ef` fetched/stored one clean 4,429-character article, and run `5372992f73a74afda78d0a1573d00790` found one already-known article, with no content parse error or new duplicate. Status is `success` with `success_claim: true` based only on these retest results. [WCS robots.txt](https://newsroom.wcs.org/robots.txt) is available.

### Smithsonian's National Zoo — `smithsonians-national-zoo`

- Official site: [Smithsonian's National Zoo](https://nationalzoo.si.edu/).
- Adapter: `archive`, the current [News](https://nationalzoo.si.edu/news) page. Parse its first-party Press Releases and Animal Stories. The [historical press-release archive](https://nationalzoo.si.edu/news/press-release-archive) is official but cannot be used while disallowed by [robots.txt](https://nationalzoo.si.edu/robots.txt); no official RSS/API was found.
- Language `en`; JS not required; observed server-rendered HTML; status `partial` because only the limited current listing is compliant. [Sitemap](https://nationalzoo.si.edu/sitemap.xml) confirms the official news paths but does not override robots policy.

### Toronto Zoo — `toronto-zoo`

- Official site: [Toronto Zoo](https://www.torontozoo.com/); [Media Room](https://www.torontozoo.com/mediaroom) and [2026 press archive](https://www.torontozoo.com/mediaroom/press2026) are server-rendered and contain dated first-party releases.
- Adapter: `archive`, follow year links from `mediaroom/press2026` and restrict details to `/mediaroom/`.
- Language `en`; JS not required; observed server-rendered HTML; status `blocked`. [robots.txt](https://www.torontozoo.com/robots.txt) returns HTTP 404, so the fail-closed Fetcher must not crawl it yet.

### Singapore Zoo — `singapore-zoo`

- Official park page: [Singapore Zoo on Mandai](https://www.mandai.com/en/singapore-zoo.html); official [Media Centre](https://www.mandai.com/en/about-mandai/media-centre.html).
- Adapter: `sitemap`, [Mandai English sitemap](https://www.mandai.com/content/wrs/en.sitemap.xml), filtered to `/en/about-mandai/media-centre/` article URLs. The article pages are first-party HTML. The Media Centre index uses a JS filter servlet under `/bin/`; [robots.txt](https://www.mandai.com/robots.txt) disallows `/bin/`, and direct calls were not a reliable source.
- Language `en`; JS required for the listing shell (article pages are SSR); status `partial`. The root [sitemap](https://www.mandai.com/sitemap.xml) is also official and points to the same content family.

### Ueno Zoo — `ueno-zoo`

- Official site: [Ueno Zoo](https://www.tokyo-zoo.net/ueno/index.html), operated in the Tokyo Zoo Net family. The Japanese [news archive](https://www.tokyo-zoo.net/ueno/news/index.html) and English [news mirror](https://www.tokyo-zoo.net/en/ueno/news/index.html) are server-rendered.
- Adapter: `archive`, Japanese `/ueno/news/` listing and detail paths; English mirror can be a language-specific alternative. [Sitemap](https://www.tokyo-zoo.net/sitemap.xml) is a navigation aid; no RSS was found.
- Language `ja`; JS not required; observed server-rendered HTML; status `blocked`. [robots.txt](https://www.tokyo-zoo.net/robots.txt) returns HTTP 404, so fail-closed fetching cannot proceed.

### Taipei Zoo — `taipei-zoo`

- Official municipal site: [Taipei Zoo](https://www.zoo.gov.taipei/), with an [English site](https://english.zoo.gov.taipei/Default.aspx).
- Adapter: `rss`, [official RSS endpoint](https://www.zoo.gov.taipei/OpenData.aspx?SN=8D46BA90548DAD4E), with [official JSON](https://www.zoo.gov.taipei/OpenData.aspx?SN=022A4E6F1C7F323A) or [XML](https://www.zoo.gov.taipei/OpenData.aspx?SN=1B8CDF622E1AD51D) fallback. The [Chinese news archive](https://www.zoo.gov.taipei/News.aspx?n=BD065B2FA7782989&sms=72544237BBE4C5F6) is server-rendered.
- Language primary `zh` (observed tag `zh-Hant`); JS not required; observed server-rendered HTML plus official RSS/XML/JSON; status `blocked`. [Chinese robots.txt](https://www.zoo.gov.taipei/robots.txt) and [English robots.txt](https://english.zoo.gov.taipei/robots.txt) both return HTTP 404, so fail-closed fetching blocks the otherwise strong sources.

### Taronga Zoo Sydney — `taronga-zoo-sydney`

- Official operator site: [Taronga](https://www.taronga.org.au/), including the [Sydney Zoo page](https://www.taronga.org.au/sydney-zoo/). Official [Newsroom](https://www.taronga.org.au/about/newsroom) and [media releases](https://www.taronga.org.au/about/newsroom/media-releases) are available.
- Adapter: `api`, [first-party article-search JSON](https://www.taronga.org.au/api/SearchApi/article-search?page=1&pageSize=9&pageId=15358). The endpoint also covers Taronga Western Plains Zoo Dubbo and the conservation society; the checked page yielded zero verified Sydney-specific candidates and no Sydney filter contract was verified. [Sitemap](https://www.taronga.org.au/sitemap.xml) is registry evidence only.
- Language `en`; JS observed for result loading (SSR newsroom shell plus first-party API); status `partial`/unsupported. [robots.txt](https://www.taronga.org.au/robots.txt) has no disallow rule, but no safe Sydney identity filter is available.

### São Paulo Zoo — `sao-paulo-zoo`

- Official site: [Zoo São Paulo](https://www.zoologico.com.br/), with [English home](https://www.zoologico.com.br/en). The [press page](https://www.zoologico.com.br/noticias) is a Next.js first-party page.
- Adapter: `api`, [first-party press API](https://www.zoologico.com.br/v1/press?language=pt&tag=INTERNAL_NEWS&type=desktop&order=DESC). Send the public `x-api-key`, `x-source-id`, and `x-page-index` headers exposed by the official page bundle; retain `INTERNAL_NEWS` only and reject external links.
- Language primary `pt` (observed tag `pt-BR`); JS required (Next.js page plus API); status `blocked`. [www robots.txt](https://www.zoologico.com.br/robots.txt) and [canonical robots.txt](https://zoologico.com.br/robots.txt) both return HTTP 404, so fail-closed fetching blocks the API.

### National Zoological Garden — `national-zoological-garden`

- Official site: [National Zoological Garden, Pretoria](https://www.pretoriazoo.org/); the institutional [SANBI zoo page](https://www.sanbi.org/gardens/zoo/) redirects/identifies the same official garden.
- Adapter evidence: [WordPress RSS](https://www.pretoriazoo.org/feed/) and [WordPress REST API](https://www.pretoriazoo.org/wp-json/wp/v2/posts?per_page=10&_fields=id,date,modified,link,slug,title,content,categories) both expose first-party root-level article slugs. No stable `/news/` or `/article/` path was verified, so the broad root-slug rule is unsafe and both source plans are disabled pending a narrower URL policy. [Newsroom](https://www.pretoriazoo.org/homepage/newsroom/) is identity evidence only.
- Language `en`; JS not required; observed WordPress SSR plus RSS/API; status `partial`/unsupported. [robots.txt](https://www.pretoriazoo.org/robots.txt) allows the endpoints, but does not make the broad root-slug candidate rule safe.

## Gaps and operational blockers

- `toronto-zoo`, `ueno-zoo`, `taipei-zoo`, and `sao-paulo-zoo`: official source pages are present, but missing `robots.txt` (HTTP 404) requires fail-closed blocking until each host publishes a valid policy.
- `smithsonians-national-zoo`: current `/news` is usable, but historical archive paths are explicitly disallowed by the official robots policy; no official RSS/API was found.
- `singapore-zoo`: the current Mandai Media Centre index is JS-gated and its `/bin/` servlet is robots-disallowed; the filtered official sitemap provides a partial compliant source.
- `taronga-zoo-sydney`: the first-party API and sitemap are operator-wide; the checked response yielded zero verified Sydney-specific candidates, so no source is enabled.
- `national-zoological-garden`: verified RSS/API links are root-level slugs without a stable news/article prefix; both sources are disabled rather than using a broad root-slug allow rule.
- `san-diego-zoo`: the official Alliance sitemap contains stale Pantheon-host locations; use the Story Hub archive and canonicalize/filter URLs instead.
- `bronx-zoo`: the WCS source remains operator-wide and only eight of 25 observed feed items passed the Bronx identity filter. Keep `.article-body` source-scoped; the two clean live retests now support the source's success claim.

There are no identity/no-source gaps: all ten authoritative roster rows have a verified official identity and at least one official source candidate.
