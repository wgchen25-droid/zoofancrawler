# Core Global adapter plans

Checked: 2026-08-14 (Europe/Berlin)  
Roster: `data/zoofan_phase0_zoo_list_v0_1.md`  
Roster SHA256: `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`

This is the conservative adapter handoff for the ten rows in both `core30`
and `global_top10`. It records only official zoo/operator/institution URLs.
Selectors and candidate counts are direct-response observations from the
checked date; they are not guarantees of future volume.

Missing or HTTP-404 `robots.txt` is fail-closed. Probe counts for a blocked
source are evidence only. No browser automation, robots bypass, synthesized
article URL, or speculative selector is proposed; the Bronx `.article-body`
selector is recorded only because the bounded official article probe matched
it directly and the two clean live retests verified it.

## Status summary

Row status: 2 supported, 4 partial, 4 robots-blocked. Across 20 source plans:
2 supported, 2 partial, 8 `robots_disallowed`, 2 `javascript_required`, 3
`unsupported`, and 3 `registry_only`.

| Stable ID | Exact roster name | Official host | Row status | Main constraint |
|---|---|---|---|---|
| `san-diego-zoo` | San Diego Zoo | `zoo.sandiegozoo.org` | supported | Alliance Story Hub is on the operator host |
| `bronx-zoo` | Bronx Zoo | `bronxzoo.com` | supported | WCS feed is operator-wide; require Bronx identity text filter; `.article-body` passed live retest |
| `smithsonians-national-zoo` | Smithsonian's National Zoo | `nationalzoo.si.edu` | partial | current 16-card listing only; historical paths disallowed |
| `toronto-zoo` | Toronto Zoo | `www.torontozoo.com` | robots_disallowed | robots.txt HTTP 404 |
| `singapore-zoo` | Singapore Zoo | `www.mandai.com` | partial | Mandai-wide sitemap; index JS/API is not a zoo-specific contract |
| `ueno-zoo` | Ueno Zoo | `www.tokyo-zoo.net` | robots_disallowed | robots.txt HTTP 404 |
| `taipei-zoo` | Taipei Zoo | `www.zoo.gov.taipei` | robots_disallowed | Chinese and English robots.txt HTTP 404 |
| `taronga-zoo-sydney` | Taronga Zoo Sydney | `www.taronga.org.au` | partial | Sydney/Dubbo filter was not verified |
| `sao-paulo-zoo` | São Paulo Zoo | `www.zoologico.com.br` | robots_disallowed | robots.txt HTTP 404; internal API links are null |
| `national-zoological-garden` | National Zoological Garden | `www.pretoriazoo.org` | partial | root-level article slugs have no safe narrow news/article path |

## Source plans and observed candidates

`D` is the raw direct endpoint candidate count. `S` is the count after the
named verified selector/path filter. A dash means no safe selector was
verified. `max_pages` and `max_sitemaps` are deliberately never above 2.

| Stable ID | Source ID | Kind / status | URL and official host | Narrow allow / exclude | Verified selectors or fields | D / S | Bound |
|---|---|---|---|---|---|---:|---:|
| `san-diego-zoo` | `san-diego-zoo-story-hub` | `archive` / supported | [Story Hub](https://sandiegozoowildlifealliance.org/story-hub/search-news) · `sandiegozoowildlifealliance.org` | allow `/PR/` and `/pressroom/news-releases/`; exclude `/story-hub/`, external hosts | `div.newsroom-lists`; link `div.headline a[href]`; title `div.headline h3`; date `div.date time[datetime]`; next `nav[aria-labelledby="pagination-heading"] a[rel="next"]` | 10 / 10 | `max_pages: 2` |
| `bronx-zoo` | `bronx-zoo-wcs-rss` | `rss` / supported | [WCS RSS](https://newsroom.wcs.org/DesktopModules/DnnForge%20-%20NewsArticles/Rss.aspx?TabID=13614&ModuleID=28242&MaxCount=25) · `newsroom.wcs.org` | allow `/News-Releases/articleType/ArticleView/`; exclude external hosts and non-detail WCS paths; require `Bronx Zoo` or `Bronx, NY` text (8 of 25 observed items) | XML `item`, `title`, `link`, `description`, `pubDate`; article content `.article-body` (verified live) | 25 / 25 | feed endpoint |
| `smithsonians-national-zoo` | `smithsonians-national-zoo-current-news` | `archive` / partial | [current News](https://nationalzoo.si.edu/news) · `nationalzoo.si.edu` | allow `/news/`, `/animals/news/`; exclude `/news/archive`, `/news/press-release-archive`, contact form | combined card blocks 16/15; link `div.p-4.pr-8.flex-grow > a[href]`; title `.card--title`; date `.card--tagline`; summary `.card--caption` | 16 / 16 | `max_pages: 1` |
| `smithsonians-national-zoo` | `smithsonians-national-zoo-historical-archive` | `archive` / robots_disallowed | [press archive](https://nationalzoo.si.edu/news/press-release-archive) · `nationalzoo.si.edu` | no active allow; path is disallowed by robots | none claimed | — / — | probe only |
| `toronto-zoo` | `toronto-zoo-press-2026` | `archive` / robots_disallowed | [2026 press archive](https://www.torontozoo.com/mediaroom/press2026) · `www.torontozoo.com` | allow `/mediaroom/press2026/<slug>`; exclude `/mediaroom/`, assets, external hosts | `a.press-item`; link `a.press-item[href]`; title `.press-title`; date `.press-content > span` | 21 / 21 | `max_pages: 1` |
| `singapore-zoo` | `singapore-zoo-mandai-media-sitemap` | `sitemap` / registry_only | [Mandai English sitemap](https://www.mandai.com/content/wrs/en.sitemap.xml) · `www.mandai.com` | allow `/en/about-mandai/media-centre/`; exclude index `.html`, `/bin/`, `/api/`; no Singapore-only identity filter verified | XML `loc` | 926 / 262 (261 detail) | `max_sitemaps: 1` |
| `singapore-zoo` | `singapore-zoo-mandai-media-index` | `archive` / javascript_required | [Media Centre](https://www.mandai.com/en/about-mandai/media-centre.html) · `www.mandai.com` | index shell only; exclude `/bin/`, external hosts | no server-rendered selector | 0 / 0 | no pagination |
| `singapore-zoo` | `singapore-zoo-mandai-media-api` | `json_api` / robots_disallowed + unsupported | [press-room servlet](https://www.mandai.com/bin/commons/pressRoomBlockDataServlet?pagePath=/content/wrs/en/mandai/about-mandai/media-centre/jcr:content/root/responsivegrid/pressroomblock) · `www.mandai.com` | no active allow; `/bin/` is disallowed | none; direct probe returned HTTP 500 | 0 / 0 | probe only |
| `ueno-zoo` | `ueno-zoo-news-ja` | `archive` / robots_disallowed | [Japanese news](https://www.tokyo-zoo.net/ueno/news/index.html) · `www.tokyo-zoo.net` | allow `/ueno/news/<numeric>/index.html`; exclude event items via the verified category selector, assets, external hosts | `.el-news__item[data-category="news"]`; link `a.el-news__item__inner[href]`; title `.el-news__item__heading-text`; date `.el-news__item__date` | 76 / 76 | `max_pages: 1` |
| `taipei-zoo` | `taipei-zoo-press-rss` | `rss` / robots_disallowed | [official RSS](https://www.zoo.gov.taipei/OpenData.aspx?SN=8D46BA90548DAD4E) · `www.zoo.gov.taipei` | allow `News_Content.aspx` with query `n=BD065B2FA7782989`; exclude OpenData/listing paths and external hosts | XML `item`, `title`, `link`, `description`, `pubDate` | 50 / 50 | feed endpoint |
| `taipei-zoo` | `taipei-zoo-press-json` | `json_api` / robots_disallowed | [official JSON](https://www.zoo.gov.taipei/OpenData.aspx?SN=022A4E6F1C7F323A) · `www.zoo.gov.taipei` | allow `News_Content.aspx` with query `n=BD065B2FA7782989`; exclude OpenData/listing paths and external hosts | root `$`; title `title`; URL `Source`; date `日期時間`; body `內容` | 50 / 50 | one response |
| `taipei-zoo` | `taipei-zoo-press-xml` | `unsupported` / unsupported | [official XML](https://www.zoo.gov.taipei/OpenData.aspx?SN=1B8CDF622E1AD51D) · `www.zoo.gov.taipei` | no active allow; retain official endpoint as evidence only | no generic XML selector contract claimed | 50 / — | probe only |
| `taipei-zoo` | `taipei-zoo-press-archive` | `archive` / robots_disallowed | [press archive](https://www.zoo.gov.taipei/News.aspx?n=BD065B2FA7782989&sms=72544237BBE4C5F6) · `www.zoo.gov.taipei` | allow `News_Content.aspx` with query `n=BD065B2FA7782989`; exclude listing/OpenData paths and external hosts | `#CCMS_Content a[href*="News_Content.aspx"]`; link same anchor | 20 / 20 | `max_pages: 2` (page 2 verified) |
| `taipei-zoo` | `taipei-zoo-sitemap-health` | `registry_only` / registry_only | [sitemap](https://www.zoo.gov.taipei/sitemap.xml) · `www.zoo.gov.taipei` | allow `News_Content.aspx`; exclude category/listing paths | XML `loc`; no matching article locs | 102 / 0 | `max_sitemaps: 1` |
| `taronga-zoo-sydney` | `taronga-zoo-sydney-article-api` | `json_api` / partial + unsupported | [article API](https://www.taronga.org.au/api/SearchApi/article-search?page=1&pageSize=9&pageId=15358) · `www.taronga.org.au` | allow `/about/newsroom/posts/` and `/about/newsroom/media-releases/`; exclude external hosts; Sydney/Dubbo identity filter not verified | `result.items`; title `header`; URL `link.href`; date `articleDate`; summary `summary` | 9 / 9 (verified Sydney-safe 0) | `max_pages: 2` |
| `taronga-zoo-sydney` | `taronga-zoo-sydney-news-sitemap` | `sitemap` / registry_only | [sitemap](https://www.taronga.org.au/sitemap.xml) · `www.taronga.org.au` | allow newsroom post/release paths; exclude external hosts; no Sydney-only filter verified | XML `loc` | 2,830 / 2,446 (Sydney-safe 0) | `max_sitemaps: 1` |
| `sao-paulo-zoo` | `sao-paulo-zoo-press-api` | `json_api` / robots_disallowed + unsupported | [press API](https://www.zoologico.com.br/v1/press?language=pt&tag=INTERNAL_NEWS&type=desktop&order=DESC) · `www.zoologico.com.br` | no active allow; reject external links and syntheses | `data`; title `title`; URL `link` (null in six observed records); date `publication_date`; body `content`; tag `tag` | 6 / 6 (canonical 0) | `max_pages: 2` |
| `sao-paulo-zoo` | `sao-paulo-zoo-press-page` | `archive` / javascript_required | [press page](https://www.zoologico.com.br/noticias) · `www.zoologico.com.br` | index shell only; exclude `/_next/`, API, external hosts | no static article selector | 0 / 0 | no pagination |
| `national-zoological-garden` | `national-zoological-garden-rss` | `rss` / unsupported | [WordPress RSS](https://www.pretoriazoo.org/feed/) · `www.pretoriazoo.org` | no active allow; root-slug links have no safe narrow news/article path; source disabled | XML `item`, `title`, `link`, `description`, `content:encoded`, `pubDate` | 5 / — (probe only) | disabled |
| `national-zoological-garden` | `national-zoological-garden-wp-api` | `json_api` / unsupported | [WordPress API](https://www.pretoriazoo.org/wp-json/wp/v2/posts?per_page=10&_fields=id,date,modified,link,slug,title,content,categories) · `www.pretoriazoo.org` | no active allow; root-slug links have no safe narrow news/article path; source disabled | root `$`; title `title.rendered`; URL `link`; date `date`; updated `modified`; body `content.rendered` | 10 / — (probe only) | disabled |

## Row-specific decisions

### San Diego Zoo — `san-diego-zoo`

Use the Alliance Story Hub archive. Its ten current cards have verified
`div.newsroom-lists`/headline/time selectors and a verified next-page link.
The visitor site and the press host are both official; the source host in the
plan is `sandiegozoowildlifealliance.org`. The Alliance sitemap is deliberately
not an adapter source because its checked `loc` set includes stale legacy-host
locations.

### Bronx Zoo — `bronx-zoo`

Use the official WCS RSS feed, but keep the required Bronx identity filter.
The operator-wide feed has 25 `item` elements and 25 links; eight records
matched the observed `Bronx Zoo`/`Bronx, NY` text hint. Those eight are the
Bronx identity result, not a structured WCS category, so no broader WCS URL
set is allowed. A bounded diagnostic fetched the official article
[probe URL](https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/26530/As-Arctic-Sea-Ice-Shrinks-Polar-Bears-Spend-More-Time-on-Landand-More-Time-Consuming-Seabird-Eggs.aspx)
with HTTP 200: `.article-body` is the exact content root and neither `main`
nor `article` exists. Record `.article-body` only on this source. The live
retest runs `d1dc9a0cfc15474dbcb322d9e73b97ef` and
`5372992f73a74afda78d0a1573d00790` discovered eight identity matches; the
first fetched/stored one clean 4,429-character article and the second found
one already known, with no content parse error or new duplicate. This supports
`supported` and `success_claim: true` for the source. Earlier bounded smoke
artifacts remain diagnostic history: `/tmp/zoofan-phase0.9Jx9xK/global_top10.pass1.{json,log}`
and `global_top10.pass2.{json,log}`.

### Smithsonian's National Zoo — `smithsonians-national-zoo`

The current [News](https://nationalzoo.si.edu/news) response has eight press
cards and eight animal-story cards. The exact card selectors are recorded, but
the historical `/news/archive` and `/news/press-release-archive` paths are
robots-disallowed. The plan therefore remains latest-listing partial.

### Toronto Zoo — `toronto-zoo`

The official 2026 press page has 21 `a.press-item` cards with title/date
children. This is a probe-only adapter plan because the official
[robots.txt](https://www.torontozoo.com/robots.txt) returns HTTP 404.

### Singapore Zoo — `singapore-zoo`

The Mandai English sitemap has 926 `loc` entries, of which 262 are under the
verified Media Centre path and 261 are detail URLs after excluding the index.
The source is retained as `registry_only`: the checked evidence did not provide
a Singapore-Zoo-only identity filter for this operator-wide media centre. The
HTML index is a JS shell with zero server-rendered cards, and its `/bin/`
servlet is robots-disallowed and returned HTTP 500 to a direct probe.

### Ueno Zoo — `ueno-zoo`

The Japanese archive exposes 76 `data-category="news"` items and 28 event
items; the proposed selector excludes events. The English mirror is evidence
only. The host's robots endpoint is HTTP 404, so no crawl is enabled.

### Taipei Zoo — `taipei-zoo`

The official RSS and JSON endpoints each expose 50 current records, and the
HTML press archive exposes 20 first-page `News_Content.aspx` links with a
verified page-2 control. The sitemap has 102 locations but zero matching
article URLs after the narrow filter, so it is `registry_only`. All Chinese and
English host plans remain robots-blocked because their robots endpoints return
HTTP 404.

### Taronga Zoo Sydney — `taronga-zoo-sydney`

The first-party JSON API exposes verified fields (`result.items`, `header`,
`link.href`, `articleDate`, `summary`) with nine items on page 1 and 272 total
pages, but zero checked candidates were verified as Sydney-specific. The API
and 2,446-entry newsroom sitemap cover both Sydney and Dubbo, and no
Sydney-specific query/filter contract was verified. Those plans remain
partial/registry-only; no guessed category parameter is introduced.

### São Paulo Zoo — `sao-paulo-zoo`

The official Next.js page has no server-rendered press cards. The documented
first-party API returns six `INTERNAL_NEWS` records per page, but all six
observed `link` values are null. The adapter must not construct URLs from IDs
or use external `EXTERNAL_NEWS` links. Both official robots aliases are HTTP
404, so the plan is unsupported and robots-blocked.

### National Zoological Garden — `national-zoological-garden`

The dedicated WordPress RSS feed has five official items. The REST API returns
ten root posts, with `X-WP-TotalPages: 3` and a verified next-page header. Both
sets use root-level article slugs without a stable `/news/` or `/article/`
prefix. Because a broad root-slug allow rule is not safe, both sources are
unsupported/disabled pending a narrower verified path or source-specific URL
policy; no broad rule is configured.

## Gaps and handoff rules

- Do not enable `toronto-zoo`, `ueno-zoo`, `taipei-zoo`, or `sao-paulo-zoo`
  until each official host serves a valid robots policy.
- Keep Smithsonian's historical paths blocked while its robots policy lists
  them as disallowed.
- Keep Mandai's operator-wide sitemap and Taronga's operator-wide API/sitemap
  as registry/partial evidence until zoo-specific identity filters are
  directly verified.
- Keep National Zoological Garden's RSS/API as disabled evidence until a
  narrow verified article path exists; do not restore the root-slug allow rule.
- Keep São Paulo's API unsupported until it exposes canonical official URLs or
  another official detail URL contract; never synthesize URLs from UUIDs.
- Keep Bronx's WCS feed operator-wide and preserve the eight-entry identity
  filter; the clean live retests verify the source-scoped `.article-body`
  selector without turning the feed into a WCS-wide Bronx category.
- There are no identity/source-not-found gaps: every roster row has an official
  identity and at least one direct source candidate, even where execution is
  currently blocked or limited.

The complete source objects, selector strings, URL policies, robots evidence,
and candidate counts are in
[`core-global-adapters.json`](core-global-adapters.json).
