# Core Europe adapter plans

Checked on **2026-08-14**. Scope is exactly the 10 Europe rows in the authoritative Phase 0 roster:

- roster: [`data/zoofan_phase0_zoo_list_v0_1.md`](../../data/zoofan_phase0_zoo_list_v0_1.md)
- roster SHA-256: `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`
- groups: `core30`, `europe_top10`
- tier: `Core` for every row

The companion JSON contains copyable source records using the current schema (`id`, `language`, `name`, `kind`, `url`, `config`). `archive` selectors were checked against the fetched HTML with the project parser; `max_pages` is never greater than 2. `sitemap` plans use exact official XML paths and narrow `allow_regex` rules.

Validation passed: JSON parsing, current `validate_config` schema validation for all 29 plans, CSS selector compilation, and regex compilation. The 12 ready archive plans were re-run through `parse_archive` with their bounded listing page limits, and the 9 sitemap endpoints were probed with their recorded allow rules; only 6 sitemap fallbacks remain enabled after the max-sitemaps cap. Prague, Burgers, and Copenhagen article-content selector findings below are final bounded live retests, with success claims limited to the exact source-scoped selectors and recorded run evidence. Endpoint status and candidate counts below are from direct official HTTP/XML/HTML probes; blocked or no-candidate endpoints remain disabled.

## Plan summary

| Stable ID | Exact roster name | Official host | Enabled plans | Candidate evidence | Constraint/gap |
|---|---|---|---|---:|---|
| `chester-zoo` | Chester Zoo | `www.chesterzoo.org` | news archive | 12/page, 24 at max 2 pages | Sitemap locs are all an alternate host and is disabled; no RSS/API |
| `zsl-london-zoo` | ZSL London Zoo | `www.londonzoo.org` | press registry only | 0 | News/blog/sitemap direct probes returned Cloudflare 403; no bypass |
| `prague-zoo` | Prague Zoo | `www.zoopraha.cz` | English news archive + Czech sitemap fallback + press registry | 10/page, 20 at max 2 pages; sitemap 5,198 | No RSS/API; sitemap fallback is Czech |
| `tiergarten-schonbrunn` | Tiergarten Schönbrunn | `www.zoovienna.at` | German news + German press archive | 15/page, 30 at max 2 pages each | No verified sitemap/RSS/API; English archive sparse |
| `pairi-daiza` | Pairi Daiza | `www.pairidaiza.eu` | English news + English news sitemap fallback | 8 first-page cards; sitemap 68 | No raw-HTML pagination, RSS/API, or public press archive |
| `diergaarde-blijdorp` | Diergaarde Blijdorp | `diergaardeblijdorp.nl` | English media + English sitemap fallback | 10 first-page cards; sitemap 42 | Page links lack a safe next relation; `/api` robots-disallowed |
| `burgers-zoo` | Burgers' Zoo | `www.burgerszoo.com` | English news + sitemap fallback | 9 first-page cards; sitemap 221 | Load more has no validated href/API; no RSS/API |
| `copenhagen-zoo` | Copenhagen Zoo | `www.zoo.dk` | Danish news + sitemap fallback + press registry | 12 first-page cards; sitemap 176 | No English news archive; `/api/*` robots-disallowed |
| `zoo-zurich` | Zoo Zürich | `www.zoo.ch` | German Zoonews + media + media archive + sitemap fallback | 20/page, 40 at max 2 pages; media 5/26; sitemap 630 | English Zoonews sparse; no RSS/API |
| `zooparc-de-beauval` | ZooParc de Beauval | `www.zoobeauval.com` / `actus.zoobeauval.com` | English Actus + press/health registry; sitemap fallback disabled by max-sitemaps cap | 24/page, 48 at max 2 pages; bounded sitemap 0 (76 observed in an unbounded child) | Press cards target off-host documents; WP JSON not used |

Aggregate: 29 plans; 23 enabled (12 ready archives, 6 sitemap fallbacks, 5 registry-only); 6 blocked/disabled or health-only. No RSS/Atom or JSON API plan is invented.

## Bounded live content-selector diagnostics

Final retests used `/tmp/zoofan-phase0.9Jx9xK` on **2026-08-14** with the normal HTTP/robots-compliant fetcher, `max_candidates_per_source=1`, and `max_pages=1`. Prague quality runs: `fd414c1ddbcc4d01b9da4665eac7cf16` and `ceb3095732134607bbe33edf18376cdd`; Burgers archive+sitemap runs: `b9e316e67055447384923d18138b96c3` and `92bbe446849e44f6904153d3e90c80cb`; Copenhagen final runs: `ec45ad0ab68a4cecb59812ec9716a2f8` and `d87cac7c7c234d7caa0a67b1d4c2cdd1`, with prior archive source runs `b3d0b2e9c3004b2f9a45069dbbb833d6` and `eed731ad9545400f99ad722448e36a49`. Official article responses were HTTP 200; claims are limited to the exact selectors and run evidence.

| Zoo / source pages | Exact safe article root | Evidence and limitations |
|---|---|---|
| `prague-zoo-news-en` [English article](https://www.zoopraha.cz/en/about-zoo/news/16312-southern-ground-hornbill-chick-exceptionally-on-view-to-visitors-this-year) + `prague-zoo-sitemap-cs` [Czech article](https://www.zoopraha.cz/aktualne/10743-adoptujte-si-medveda) | `#maincontent .para` | Verified: pass 1 stored clean 4263/589-character articles; pass 2 found both already known with no errors. Generic `main`/`article` roots remain absent; a Czech fixture remains a limitation for fixture coverage. |
| `burgers-zoo-news-en` + `burgers-zoo-sitemap-news-en` | `main#main-content > .container-xl > .longread` | Verified: first pass stored two clean articles of 2151/2498 characters across archive+sitemap; second pass inserted/updated 0/0 with zero navigation/content/date errors. Load more has no validated static target; sitemap remains the deterministic fallback. |
| `copenhagen-zoo-news-da` [news article](https://www.zoo.dk/nyheder/er-der-en-pandaunge-paa-vej) + `copenhagen-zoo-sitemap-news-da` [sitemap article](https://www.zoo.dk/nyheder/arets-store-optaelling-er-netop-slut-koebenhavn-zoo-har-produceret-mere-end-119000-dyr-i-2022) | `div.generic-text_component__hr0Ih, p.news-article-header_leadingParagraph__K7SWH` | Verified with Danish date selector/pattern: final sitemap pass stored a clean 2555-character article dated 2023-01-25; second pass found both sources already known with no errors. Body may be short; `LatestNews`/`footer` are excluded, and `#app`/`/api/*` are never fallbacks. |

## Executable archive plans

The following are the active HTML plans. Every `allow_regex` is an exact official-host and article-path boundary; source/archive roots are explicitly excluded. Empty date fields were intentional when the verified cards had no date element.

| Source ID | URL | Selectors and pagination | Verified candidates |
|---|---|---|---:|
| `chester-zoo-news-archive` | [news](https://www.chesterzoo.org/news) | `.card-search` → `a[href^="/news/"]`; title `.heading-5`; next `a[title="Next page"]`; `max_pages: 2` | 12/page; 24 |
| `prague-zoo-news-en` | [English news](https://www.zoopraha.cz/en/about-zoo/news) | `#maincontent .items-intro > div` → `a[href^="/en/about-zoo/news/"]`; title `h3`; article root `#maincontent .para` (verified live); next `a[title="Next"][href*="start="]`; `max_pages: 2` | 10/page; 20 |
| `tiergarten-schonbrunn-news-de` | [German news](https://www.zoovienna.at/de/news/) | `li.dates-item` → `a.dates-item__wrapper[href^="/de/news/"]`; title `.date-content`; date `.date-label`; next `a.pagination-prev__link[href*="seite="]`; `max_pages: 2` | 15/page; 30 |
| `tiergarten-schonbrunn-press-de` | [press archive](https://www.zoovienna.at/presse/archiv) | `li.dates-item` → `a.dates-item__wrapper[href^="/presse/"]`; title `.date-content`; date `.date-label`; next `a.pagination-prev__link[href*="seite="]`; `max_pages: 2` | 15/page; 30 |
| `pairi-daiza-news-en` | [English news](https://www.pairidaiza.eu/en/news/) | `li.archive-item .card-news` → `a[href^="/en/news/"]`; title `h3.card-news__content__title-inner`; date `span.t-label`; no pagination selector; `max_pages: 1` | 8 |
| `diergaarde-blijdorp-media-en` | [English media](https://diergaardeblijdorp.nl/en/media) | `.min-h-media-card` → `a[href^="/en/media/"]`; title `h3`; date `span.mt-auto.text-white`; no pagination selector; `max_pages: 1` | 10 |
| `burgers-zoo-news-en` | [English news](https://www.burgerszoo.com/news) | `article.block-news-item` → `a[href^="/news/20"]`; title `h2.card-title`; date `p.card-date`; article root `main#main-content > .container-xl > .longread` (verified live); no pagination selector; `max_pages: 1` | 9 |
| `copenhagen-zoo-news-da` | [Danish news](https://www.zoo.dk/nyheder) | `a.news-card_component__c1UR7` → `a[href]`; title `h4.news-card_headline__cDLt0`; date `span.news-card_date__oOfbb`; article root `div.generic-text_component__hr0Ih, p.news-article-header_leadingParagraph__K7SWH` (verified live); article date `p.news-article-header_time__ooR7a` with Danish month pattern; no pagination selector; `max_pages: 1` | 12 |
| `zoo-zurich-zoonews-de` | [German Zoonews](https://www.zoo.ch/de/zoonews) | `#paragraph--1750 .grid__item > .home-tile` → `a.tile-teaser__link[href^="/de/zoonews/"]`; title `h2.tile-teaser__title-mobile`; date `time[datetime]`; next `a[rel="next"][href*="page="]`; `max_pages: 2` | 20/page; 40 |
| `zoo-zurich-media-de` | [current media](https://www.zoo.ch/de/medien) | `a.teaser--search[href*="/de/medien/medienmitteilung/"]`; title `h2.teaser__title`; date `time[datetime]`; `max_pages: 1` | 5 |
| `zoo-zurich-media-archive-de` | [media archive](https://www.zoo.ch/de/medien/archiv-medienmitteilungen) | `a.teaser--search[href*="/de/medien/medienmitteilung/"]`; title `h2.teaser__title`; date `time[datetime]`; `max_pages: 1` | 26 |
| `zooparc-de-beauval-actus-en` | [English Actus](https://actus.zoobeauval.com/en/) | `article.c-card` → `a.c-card__link[href^="https://actus.zoobeauval.com/en/"]`; title `.c-card__title`; date `time.u-text-tag`; next `a[aria-label="Next articles"]`; allow only single-segment `/en/<slug>/` article paths; `max_pages: 2` | 24/page; 48 |

## Sitemap and registry plans

| Source ID | Kind / status | Exact URL and allow boundary | Verification |
|---|---|---|---|
| `chester-zoo-sitemap-reject-noncanonical` | `sitemap`, disabled | [sitemap.xml](https://www.chesterzoo.org/sitemap.xml); allow only `^https://www\.chesterzoo\.org/news/…$` | HTTP 200, 894 locs, 0 candidates after host policy; locs resolve to an alternate host |
| `chester-zoo-press-registry` | `registry_only` | [press/media](https://www.chesterzoo.org/footer/press-and-media) | HTTP 200; contact page, 0 article candidates |
| `zsl-london-zoo-news-blocked` | `archive`, disabled | [news](https://www.londonzoo.org/zoo-stories/news) | HTTP 403 direct probe; no selectors guessed |
| `zsl-london-zoo-blog-blocked` | `archive`, disabled | [blog](https://www.londonzoo.org/zoo-stories/blog) | HTTP 403 direct probe; no selectors guessed |
| `zsl-london-zoo-sitemap-blocked` | `sitemap`, disabled | [sitemap.xml](https://www.londonzoo.org/sitemap.xml); allow only `/zoo-stories/news/` or `/zoo-stories/blog/` | Advertised by robots, direct HTTP 403 |
| `zsl-london-zoo-press-registry` | `registry_only` | [ZSL press](https://www.zsl.org/about-zsl/press) | Official institutional press office; no release-list contract |
| `prague-zoo-sitemap-cs` | `sitemap`, fallback | [sitemap.xml](https://www.zoopraha.cz/sitemap.xml); allow only `^https://www\.zoopraha\.cz/aktualne/…$`; article root `#maincontent .para` verified live | HTTP 200, 9,539 locs, 5,198 Czech candidates |
| `prague-zoo-press-registry` | `registry_only` | [contacts](https://www.zoopraha.cz/en/about-zoo/142-contacts) | HTTP 200; public-relations information, no release list |
| `pairi-daiza-sitemap-news-en` | `sitemap`, fallback | [English news child sitemap](https://www.pairidaiza.eu/en/wp-sitemap-posts-news-1.xml); allow only `/en/news/…` | HTTP 200, 68 locs, 68 candidates |
| `diergaarde-blijdorp-sitemap-media-en` | `sitemap`, fallback | [sitemap.xml](https://diergaardeblijdorp.nl/sitemap.xml); allow only `/en/media/…` | HTTP 200, 572 locs, 42 candidates |
| `burgers-zoo-sitemap-news-en` | `sitemap`, fallback | [sitemap.xml](https://www.burgerszoo.com/sitemap.xml); allow only `/news/YYYY/MM/…`; article root `main#main-content > .container-xl > .longread` (verified live) | HTTP 200, 291 locs, 221 candidates |
| `copenhagen-zoo-sitemap-news-da` | `sitemap`, fallback | [www sitemap.xml](https://www.zoo.dk/sitemap.xml); allow only `/nyheder/…`; article root `div.generic-text_component__hr0Ih, p.news-article-header_leadingParagraph__K7SWH` verified live with Danish date fields | HTTP 200 at final www URL, 404 locs, 176 candidates; no-www robots URL redirects 308 |
| `copenhagen-zoo-press-registry` | `registry_only` | [press](https://www.zoo.dk/om-zoo/presse) | HTTP 200; contact page, no release list |
| `zoo-zurich-sitemap-zoonews-de` | `sitemap`, fallback | [German sitemap.xml](https://www.zoo.ch/de/sitemap.xml); allow only `/de/zoonews/…` | HTTP 200, 2,100 locs, 630 candidates; root redirects here |
| `zooparc-de-beauval-sitemap-actus-en` | `sitemap`, disabled no-candidate fallback | [Actus sitemap index](https://actus.zoobeauval.com/sitemap_index.xml); allow only single-segment Actus `/en/<slug>/` article paths; exclude `/page`, `/categorie`, `/category`, `/tag` | HTTP 200 index; max_sitemaps=2 reaches root + first post child only, 0 candidates; second post child was separately observed with 76 article URLs but is not executable under the cap |
| `zooparc-de-beauval-press-registry` | `registry_only` | [press releases](https://www.zoobeauval.com/en/press/press-releases) | HTTP 200, 69 cards; detail targets are off-host documents, so 0 official-host candidates |
| `zooparc-de-beauval-main-sitemap-health` | `sitemap`, disabled health-only | [main sitemap](https://www.zoobeauval.com/sitemap.xml); narrow French press-detail rule | HTTP 200, 154 locs, 0 matching detail URLs |

## Explicit non-plans

- No RSS/Atom URL was promoted from a generic `/feed/` or `/rss.xml` guess. All 10 rows have empty RSS/Atom availability in the research report.
- No `/api`, WordPress REST, or hidden JSON endpoint is proposed. For Copenhagen, `#app` and `/api/*` are explicitly rejected fallbacks. Robots-disallowed or blocked API paths are recorded only as gaps.
- London Zoo remains disabled after direct HTTP 403 responses. The rendered page evidence does not justify browser automation or a robots bypass.
- Chester's advertised sitemap remains disabled because its locs are on an alternate host. Expanding `official_host` would violate the official-host boundary.
- Beauval's official press page remains registry-only because the 69 cards target off-host document objects. The first-party Actus HTML archive is executable; its sitemap-index fallback is disabled because the required article child is second and the executable `max_sitemaps=2` cap reaches only the first child.
