# Core Europe source research

Checked on **2026-08-14**. This report covers only the 10 Europe rows in the authoritative Phase 0 roster. Every URL below is on the zoo or institution's official domain; third-party pages, social profiles, and vendor-hosted press pages were excluded.

Roster source: [`data/zoofan_phase0_zoo_list_v0_1.md`](../../data/zoofan_phase0_zoo_list_v0_1.md)
Roster SHA-256: `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`

Stable IDs are deterministic slugs of the exact roster name: lowercase, Unicode NFKD transliteration to ASCII, punctuation removal, and hyphenation. They do not depend on a current website URL.

## Counts and gaps

- 10 expected rows, 10 researched, 10 unique stable IDs.
- All 10 rows are in both `core30` and `europe_top10`; all retain roster tier `Core`.
- 10/10 official home URLs and 10/10 official news/archive entry points were verified.
- 8 institutions expose verified sitemap sources (9 official endpoints, because Beauval has separate main-site and Actus sitemaps); London Zoo's sitemap is advertised but was Cloudflare-blocked in the direct probe; no sitemap was observed for Tiergarten Schönbrunn.
- No RSS/Atom or public API endpoint was verified.
- Seven institutions expose an official press/media page or archive: Chester Zoo, ZSL, Prague Zoo (contacts/public relations), Tiergarten Schönbrunn, Copenhagen Zoo, Zoo Zürich, and ZooParc de Beauval.
- Local-language archives are materially more complete for Tiergarten Schönbrunn (German), Copenhagen Zoo (Danish), and Zoo Zürich (German). ZooParc de Beauval's French Actus archive is a completeness fallback. London Zoo's sitemap is a retrieval gap, not evidence of absence.
- The raw `verified_sparse` observations for Tiergarten Schönbrunn's English news and Zoo Zürich's English Zoonews are retained as evidence and map to canonical `partial` source status in the import fragment; each zoo's configured primary language remains the research `language.primary` value.

## Bounded live content-selector diagnostics

Final bounded live retests from `/tmp/zoofan-phase0.9Jx9xK` on **2026-08-14** verified the source-scoped selectors through the normal HTTP/robots-compliant crawler (`max_candidates_per_source=1`, `max_pages=1`). Prague quality runs were `fd414c1ddbcc4d01b9da4665eac7cf16` and `ceb3095732134607bbe33edf18376cdd`; Burgers archive+sitemap runs were `b9e316e67055447384923d18138b96c3` and `92bbe446849e44f6904153d3e90c80cb`; Copenhagen sitemap/archive runs were `ec45ad0ab68a4cecb59812ec9716a2f8` and `d87cac7c7c234d7caa0a67b1d4c2cdd1`, with prior clean Copenhagen archive source evidence in `b3d0b2e9c3004b2f9a45069dbbb833d6` and `eed731ad9545400f99ad722448e36a49`. The checked official article responses were HTTP 200; success claims are limited to the exact selectors and recorded runs.

- `prague-zoo`: both the English archive article ([English article](https://www.zoopraha.cz/en/about-zoo/news/16312-southern-ground-hornbill-chick-exceptionally-on-view-to-visitors-this-year)) and Czech sitemap article ([Czech article](https://www.zoopraha.cz/aktualne/10743-adoptujte-si-medveda)) passed the quality runs with the exact safe root `#maincontent .para`: 4263/589-character articles were stored, and the second pass found both already known with no errors. The English OG summary is article-specific and `/about-zoo/` no longer produces a false missing-summary warning.
- `burgers-zoo`: both the English archive and sitemap article paths passed with the exact safe root `main#main-content > .container-xl > .longread`: the first pass stored two clean articles of 2151 and 2498 characters across the two sources, and the second pass inserted/updated 0/0 with zero navigation, content, or date errors. The Load more control remains without a validated static target; sitemap fallback and official-host rules are retained.
- `copenhagen-zoo`: both Danish news and sitemap article pages passed with the union selector `div.generic-text_component__hr0Ih, p.news-article-header_leadingParagraph__K7SWH` and Danish date fields. The archive source stored a clean article in its prior source-scoped run and was already known on the second run; the final sitemap run stored a clean 2555-character article dated 2023-01-25, and the second final run found both already known with no errors. The body may be short because `LatestNews` and `footer` content are excluded. `#app` and `/api/*` are never fallbacks.

## Roster mapping and source plan

| Stable ID | Exact roster row | Country code / city | Official site | Primary official source | Language | JS/source status |
|---|---|---|---|---|---|---|
| `chester-zoo` | Chester Zoo | GB / Chester | [chesterzoo.org](https://www.chesterzoo.org/) | [News](https://www.chesterzoo.org/news) | en | SSR, Next.js observed; no JS required |
| `zsl-london-zoo` | ZSL London Zoo | GB / London | [londonzoo.org](https://www.londonzoo.org/) | [News](https://www.londonzoo.org/zoo-stories/news) + [Blog](https://www.londonzoo.org/zoo-stories/blog) | en | Rendered listing observed; direct curl was Cloudflare-blocked, JS requirement unknown |
| `prague-zoo` | Prague Zoo | CZ / Prague | [zoopraha.cz/en](https://www.zoopraha.cz/en/) | [News](https://www.zoopraha.cz/en/about-zoo/news) | en (cs fallback) | SSR HTML; no JS required |
| `tiergarten-schonbrunn` | Tiergarten Schönbrunn | AT / Vienna | [zoovienna.at/en](https://www.zoovienna.at/en/) | [German news](https://www.zoovienna.at/de/news/) + [press archive](https://www.zoovienna.at/presse/archiv) | de (en sparse) | SSR HTML; no JS required |
| `pairi-daiza` | Pairi Daiza | BE / Brugelette | [pairidaiza.eu/en](https://www.pairidaiza.eu/en/) | [News](https://www.pairidaiza.eu/en/news/) | en (fr/nl/de available) | SSR HTML; no JS required |
| `diergaarde-blijdorp` | Diergaarde Blijdorp | NL / Rotterdam | [diergaardeblijdorp.nl/en](https://diergaardeblijdorp.nl/en/) | [English media](https://diergaardeblijdorp.nl/en/media) | en (nl fallback) | SSR, Next.js observed; no JS required |
| `burgers-zoo` | Burgers' Zoo | NL / Arnhem | [burgerszoo.com](https://www.burgerszoo.com/) | [News](https://www.burgerszoo.com/news) | en (nl/de available) | SSR initial listing; no JS required for initial discovery |
| `copenhagen-zoo` | Copenhagen Zoo | DK / Copenhagen | [zoo.dk/en](https://www.zoo.dk/en) | [Danish news](https://www.zoo.dk/nyheder) | da (en site fallback) | SSR, Next.js observed; no JS required |
| `zoo-zurich` | Zoo Zürich | CH / Zurich | [zoo.ch/en](https://www.zoo.ch/en) | [German Zoonews](https://www.zoo.ch/de/zoonews) + [media](https://www.zoo.ch/de/medien) | de (en sparse, fr available) | SSR, Drupal 11 observed; no JS required |
| `zooparc-de-beauval` | ZooParc de Beauval | FR / Saint-Aignan | [zoobeauval.com/en](https://www.zoobeauval.com/en/) | [English Actus](https://actus.zoobeauval.com/en/) + [press releases](https://www.zoobeauval.com/en/press/press-releases) | en (fr richer fallback) | SSR HTML; no JS required |

## Record notes

### `chester-zoo`

- Exact roster fields: `Chester Zoo` / `GB` / `Chester`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.chesterzoo.org/](https://www.chesterzoo.org/), host `www.chesterzoo.org`.
- Source set: [news archive](https://www.chesterzoo.org/news), [press and media](https://www.chesterzoo.org/footer/press-and-media), [robots.txt](https://www.chesterzoo.org/robots.txt), and [sitemap.xml](https://www.chesterzoo.org/sitemap.xml). No RSS, Atom, or public API was verified.
- Adapter: `archive` at `/news`; follow the numbered listing pages (22 pages were observed), then detail links under `/news/`. Use the official sitemap only as a host-filtered fallback. The rendered page exposed SSR article links and pagination; Next.js was observed and JavaScript was not required.
- Evidence also includes the [official home page](https://www.chesterzoo.org/).

### `zsl-london-zoo`

- Exact roster fields: `ZSL London Zoo` / `GB` / `London`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.londonzoo.org/](https://www.londonzoo.org/), host `www.londonzoo.org`. ZSL's official institution press page is [https://www.zsl.org/about-zsl/press](https://www.zsl.org/about-zsl/press).
- Source set: [zoo news](https://www.londonzoo.org/zoo-stories/news), [zoo blog](https://www.londonzoo.org/zoo-stories/blog), [robots.txt](https://www.londonzoo.org/robots.txt), and advertised [sitemap.xml](https://www.londonzoo.org/sitemap.xml). News showed 15 pages and blog seven pages in the rendered listings. No RSS, Atom, or public API was verified.
- Adapter: `archive` for news and blog; follow their own page controls and accept only `/zoo-stories/news/` or `/zoo-stories/blog/` links. Sitemap is a fallback only when the normal Fetcher can retrieve it.
- Source status: official rendered listings exposed article links, but direct curl probes for listing/feed/sitemap returned Cloudflare HTTP 403 on the checked date. JS requirement is therefore **unknown**, not an assertion that browser automation is needed. The crawler must keep robots fail-closed and must not add browser automation as a fallback.
- Address evidence: [official how-to-get page](https://www.londonzoo.org/plan-your-visit/how-to-get-to-london-zoo).

### `prague-zoo`

- Exact roster fields: `Prague Zoo` / `CZ` / `Prague`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.zoopraha.cz/en/](https://www.zoopraha.cz/en/), host `www.zoopraha.cz`.
- Source set: [English news](https://www.zoopraha.cz/en/about-zoo/news), [contacts/public relations](https://www.zoopraha.cz/en/about-zoo/142-contacts), and [sitemap.xml](https://www.zoopraha.cz/sitemap.xml). The official [robots.txt](https://www.zoopraha.cz/robots.txt) was also checked. No RSS, Atom, public API, or separate press-release archive was verified.
- Adapter: `archive` at `/en/about-zoo/news`; follow observed `?start=10`, `?start=20`, and subsequent offset links, then detail links under `/en/about-zoo/news/`. Sitemap is a constrained fallback. SSR HTML exposed cards and pagination; no JS was required. The exact `#maincontent .para` root is verified by the quality English/Czech live retests; generic `main`/`article` roots remain absent. The English OG summary is article-specific and the `/about-zoo/` false warning is resolved.

### `tiergarten-schonbrunn`

- Exact roster fields: `Tiergarten Schönbrunn` / `AT` / `Vienna`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.zoovienna.at/en/](https://www.zoovienna.at/en/), host `www.zoovienna.at`.
- Source set: current [German news](https://www.zoovienna.at/de/news/), sparse [English news](https://www.zoovienna.at/en/news/), [press page](https://www.zoovienna.at/presse/), [press archive](https://www.zoovienna.at/presse/archiv), [English contact](https://www.zoovienna.at/en/zoo-and-visitors/contact/), and [robots.txt](https://www.zoovienna.at/robots.txt). No verified sitemap, RSS, Atom, or public API was found.
- Adapter: `archive` at German `/de/news/` and press `/presse/archiv`; follow the observed `?seite=N` older-page links. Prefer German for completeness; English news was verified but sparse/stale. SSR HTML was directly retrievable and JavaScript was not required.

### `pairi-daiza`

- Exact roster fields: `Pairi Daiza` / `BE` / `Brugelette`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.pairidaiza.eu/en/](https://www.pairidaiza.eu/en/), host `www.pairidaiza.eu`.
- Source set: [English news](https://www.pairidaiza.eu/en/news/), [prepare-your-visit/location](https://www.pairidaiza.eu/en/prepare-your-visit/), [robots.txt](https://www.pairidaiza.eu/robots.txt), and [wp-sitemap.xml](https://www.pairidaiza.eu/wp-sitemap.xml). No RSS, Atom, public API, or public press-release archive on the official host was verified.
- Adapter: `archive` at `/en/news/`; follow the nine numbered pages observed and detail links under `/en/news/`. Use the WordPress sitemap as a host/path-filtered fallback. The listing was SSR HTML and did not require JavaScript.

### `diergaarde-blijdorp`

- Exact roster fields: `Diergaarde Blijdorp` / `NL` / `Rotterdam`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://diergaardeblijdorp.nl/en/](https://diergaardeblijdorp.nl/en/), host `diergaardeblijdorp.nl`. The English pages also use the public branding “Rotterdam Zoo”; the roster name is retained exactly.
- Source set: [English media/news](https://diergaardeblijdorp.nl/en/media), [Dutch media/news](https://diergaardeblijdorp.nl/nl/media), [visit/location](https://diergaardeblijdorp.nl/en/visit), [robots.txt](https://diergaardeblijdorp.nl/robots.txt), and [sitemap.xml](https://diergaardeblijdorp.nl/sitemap.xml). No RSS, Atom, public press archive, or permitted public API was verified; robots disallows `/api`.
- Adapter: `archive` at `/en/media`; follow explicit `?page=N` links (pages 2–12 observed), then detail links under `/en/media/`. Sitemap is a constrained fallback. Next.js/SSR HTML was observed and JavaScript was not required.

### `burgers-zoo`

- Exact roster fields: `Burgers' Zoo` / `NL` / `Arnhem`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.burgerszoo.com/](https://www.burgerszoo.com/), host `www.burgerszoo.com`.
- Source set: [English news](https://www.burgerszoo.com/news), [home](https://www.burgerszoo.com/), [robots.txt](https://www.burgerszoo.com/robots.txt), and [sitemap.xml](https://www.burgerszoo.com/sitemap.xml). No RSS, Atom, public press archive, or public API was verified.
- Adapter: `archive` at `/news`; the initial page is SSR and exposes article links, while the UI uses “Load more”. Follow only a server-provided official target if one is exposed; otherwise use the official sitemap as deterministic fallback. Article paths observed under `/news/YYYY/MM/`. The exact article root `main#main-content > .container-xl > .longread` passed both enabled archive/sitemap paths in runs `b9e316e67055447384923d18138b96c3` and `92bbe446849e44f6904153d3e90c80cb`: 2151/2498 clean characters stored, then 0 inserted/updated. JavaScript is not required for initial discovery.

### `copenhagen-zoo`

- Exact roster fields: `Copenhagen Zoo` / `DK` / `Copenhagen`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.zoo.dk/en](https://www.zoo.dk/en), host `www.zoo.dk`.
- Source set: [Danish news](https://www.zoo.dk/nyheder), [Danish press](https://www.zoo.dk/om-zoo/presse), [English contact/press](https://www.zoo.dk/en/about-us/contact-us), canonical [sitemap.xml](https://zoo.dk/sitemap.xml), and [robots.txt](https://www.zoo.dk/robots.txt). No RSS or permitted public API was verified; robots disallows `/api/*`. The English `/en/news` path was not a valid archive.
- Adapter: `archive` at `/nyheder`; follow the archive's exposed pagination/year controls (15 pages were indicated), retaining Danish as source language. Use the canonical no-www sitemap as fallback and reject non-official hosts. Next.js/SSR HTML was observed and JavaScript was not required. The union selector `div.generic-text_component__hr0Ih, p.news-article-header_leadingParagraph__K7SWH` plus Danish date fields is verified for both news and sitemap article pages; it excludes `LatestNews`/`footer`, may yield a short body, and never uses `#app` or `/api/*`.

### `zoo-zurich`

- Exact roster fields: `Zoo Zürich` / `CH` / `Zurich`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.zoo.ch/en](https://www.zoo.ch/en), host `www.zoo.ch`.
- Source set: current [German Zoonews](https://www.zoo.ch/de/zoonews), sparse [English Zoonews](https://www.zoo.ch/en/zoonews), [German media](https://www.zoo.ch/de/medien), [German contact](https://www.zoo.ch/de/kontakt), and the language-prefixed [sitemap.xml](https://www.zoo.ch/de/sitemap.xml) (the root sitemap URL redirects there). No RSS, Atom, or public API was verified.
- Adapter: `archive` at `/de/zoonews`, with the media page's further-media link as a second official archive; follow filters/paging actually exposed by the page and parse `time[datetime]`/title/body. German should be primary because the official English page says most items are currently only in German. Drupal 11/SSR HTML was observed and JavaScript was not required.

### `zooparc-de-beauval`

- Exact roster fields: `ZooParc de Beauval` / `FR` / `Saint-Aignan`; groups `core30`, `europe_top10`; tier `Core`.
- Official site: [https://www.zoobeauval.com/en/](https://www.zoobeauval.com/en/), host `www.zoobeauval.com`; the official Actus subdomain is `actus.zoobeauval.com`.
- Source set: [English Actus](https://actus.zoobeauval.com/en/), [French Actus](https://actus.zoobeauval.com/), [English press area](https://www.zoobeauval.com/en/press/), [English press releases](https://www.zoobeauval.com/en/press/press-releases), [French press releases](https://www.zoobeauval.com/presse/communiques), [main sitemap](https://www.zoobeauval.com/sitemap.xml), and [Actus sitemap index](https://actus.zoobeauval.com/sitemap_index.xml). No RSS/Atom was verified. The common WordPress JSON API path was blocked/disallowed and is not proposed.
- Adapter: `archive` at English Actus plus the official press-release listing; follow only official pagination and single-segment article links under `actus.zoobeauval.com/en/<slug>/`. The Actus sitemap-index fallback is disabled because the executable max-sitemaps cap reaches only its first child, which has no article candidates. Main and Actus pages were SSR HTML and did not require JavaScript.
- Address evidence: [official access page](https://www.zoobeauval.com/en/prepare-your-visit/access).
