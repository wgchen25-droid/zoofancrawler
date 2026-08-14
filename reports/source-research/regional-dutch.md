# Regional Lab — Dutch Border source research

Checked 2026-08-14. This report covers every one of the 18 `Region=Dutch Border` rows in the authoritative `data/zoofan_phase0_zoo_list_v0_1.md`. The roster values are preserved exactly, including the three city cells encoded as `null` because the roster cells are blank, and the spelling `GaiaZOO`.

All records have `country_code=NL` and the exact groups `regional_lab` + `dutch_border`. A source URL is included only when it is on the official zoo/operator host or is an official municipal/institution page. Search results and directories are recorded separately as discovery hints and are not recommended crawl sources.

## Roster integrity and counts

The SHA-256 of the complete authoritative roster file is
`6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`.

Hash input: all 92 LF-terminated lines (5220 bytes), including headings and
the final LF, from `data/zoofan_phase0_zoo_list_v0_1.md`.

| Measure | Count |
|---|---:|
| Roster rows | 18 |
| Priority | 2 |
| Secondary | 7 |
| Directory | 9 |
| Official zoo/operator sites verified | 14 |
| Official news/feed/sitemap sources verified | 13 |
| `rss` proposals | 11 |
| `archive` proposals | 2 |
| `registry_only` proposals | 5 |
| Blank city cells preserved | 3 |
| Rows with explicit identity/source gaps | 5 |

The five gaps are `Mini ZOO Venray` (no primary site or operator identity found), `Dierenpark 't Goor` (official site indexed but direct requests returned Cloudflare 403 and no feed was verified), `Kleindierwandelpark Hedel` (municipal identity only), `Dierenpark De Bontekoe` (official Borne records do not unambiguously match the exact roster label), and `Mini animal park Deventer` (municipal map label only). None has been omitted or given an invented URL.

## Roster and adapter matrix

| Stable ID | Exact roster name | Exact roster city | Tier | Official site / host | Language | JS | Proposed adapter | Source status |
|---|---|---|---|---|---|---|---|---|
| `gaiazoo` | GaiaZOO | Kerkrade | Priority | [gaiazoo.nl](https://www.gaiazoo.nl/) | `nl` | no | preserved `rss` + `archive` (`/nieuws/<slug>` only); root probe disabled | official host/feed observed; safe coverage not claimed |
| `kasteelpark-born` | Kasteelpark Born | Born | Secondary | [kasteelparkborn.nl](https://kasteelparkborn.nl/dierenpark/) | `nl` | no | `archive` (disabled malformed-link plan) | official page observed; safe coverage not claimed |
| `mondo-verde` | Mondo Verde | Landgraaf | Secondary | [wereldtuinenmondoverde.nl](https://www.wereldtuinenmondoverde.nl/nl/) | `nl` | no | `rss` (disabled root-slug policy) | official feed observed; safe coverage not claimed |
| `brook-valley-zoo` | Brook Valley Zoo | Stramproy | Secondary | [brookvalleyzoo.com](https://brookvalleyzoo.com/) | `nl` | no | `rss` (ready/recommended) | verified live; 1531-char article |
| `onze-beestenboel` | Onze Beestenboel | Venlo | Directory | [beestenboelvenlo.nl](https://www.beestenboelvenlo.nl/) | `nl` | no | `rss` | verified live, feed stale |
| `het-dierenrijck` | Het DierenRijck | Reuver | Directory | [dierenrijck.nl](https://dierenrijck.nl/) | `nl` | no | `rss` (ready/recommended) | verified live; 866-char article |
| `hertenspoor-zoo` | Hertenspoor Zoo | Echt | Directory | [dierenpark-echt.nl](http://www.dierenpark-echt.nl/) | `nl` | no | `rss` | verified HTTP-only, feed stale |
| `mini-zoo-venray` | Mini ZOO Venray | Venray | Directory | none verified | unknown | unknown | `registry_only` | no official site verified |
| `apenheul` | Apenheul | Apeldoorn | Priority | [apenheul.nl](https://www.apenheul.nl/) | `nl` | no | `rss` | verified live |
| `hof-van-eckberge` | Hof van Eckberge | Eibergen | Secondary | [hofvaneckberge.nl](https://www.hofvaneckberge.nl/) | `nl` | no | `rss` | verified live |
| `uilen-en-dierenpark-de-paay` | Uilen- en Dierenpark De Paay | Beesd | Secondary | [depaay.nl](https://depaay.nl/) | `nl` | no | `rss` | verified live, feed stale |
| `dierenpark-t-goor` | Dierenpark 't Goor | Brummen | Secondary | [dierenparkhetgoor.nl](https://www.dierenparkhetgoor.nl/) | `nl` | no | `registry_only` | official indexed site, direct 403 |
| `kleindierwandelpark-hedel` | Kleindierwandelpark Hedel | Hedel | Directory | none verified | `nl` | unknown | `registry_only` | municipal identity only |
| `taman-indonesia` | Taman Indonesia | Kallenkote | Secondary | [taman-indonesia.nl](https://www.taman-indonesia.nl/) | `nl` | no | `rss` | verified live |
| `bird-park-de-lorkeershoeve` | Bird Park De Lorkeershoeve | `null` (roster blank) | Directory | [vogelparkdelorkeershoeve.nl](https://www.vogelparkdelorkeershoeve.nl/) | `nl` | no | `archive` | verified live, static/stale |
| `ten-kates-dierenpark` | Ten Kate's Dierenpark | `null` (roster blank) | Directory | [dierenparktenkate.nl](https://www.dierenparktenkate.nl/) | `nl` | no | `rss` | verified live, feed stale/empty |
| `dierenpark-de-bontekoe` | Dierenpark De Bontekoe | `null` (roster blank) | Directory | none verified | `nl` | unknown | `registry_only` | official identity ambiguous |
| `mini-animal-park-deventer` | Mini animal park Deventer | Deventer | Directory | none verified | `nl` | unknown | `registry_only` | municipal label only |

## Detailed source records

### `gaiazoo` — GaiaZOO

- Roster: exact name `GaiaZOO`; country `NL`; city `Kerkrade`; tier `Priority`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.gaiazoo.nl/` / `www.gaiazoo.nl`.
- Official source URLs: [news/vlogs archive](https://www.gaiazoo.nl/nieuws-vlogs/), [news category](https://www.gaiazoo.nl/category/nieuws/), [news RSS](https://www.gaiazoo.nl/category/nieuws/feed/), [sitemap index](https://www.gaiazoo.nl/sitemap_index.xml), and [press page](https://www.gaiazoo.nl/pers/).
- Adapter: preserve the existing enabled `gaiazoo-news-rss` and `gaiazoo-news-archive` IDs with the exact anchored `^https://www\.gaiazoo\.nl/nieuws/[^/?#]+/?$` identity rule. The distinct `gaiazoo-root-slug-feed-probe` is disabled/unsupported: it observed 10 root-slug items, but that broad shape is never accepted.
- Language: `nl`; official host/feed HTTP 200 was observed, but safe article coverage is not claimed.
- Evidence: official home, archive, RSS and sitemap URLs above. Checked `2026-08-14`.

### `kasteelpark-born` — Kasteelpark Born

- Roster: exact name `Kasteelpark Born`; country `NL`; city `Born`; tier `Secondary`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://kasteelparkborn.nl/dierenpark/` / `kasteelparkborn.nl`.
- Official source URLs: [news archive](https://kasteelparkborn.nl/het-park/nieuws/), [sitemap](https://kasteelparkborn.nl/sitemap.xml).
- Adapter: `archive` from `/het-park/nieuws/`; no RSS endpoint was verified. The official page exposed 144 cards, but malformed relative links leave the plan disabled pending an origin-root rewrite. Sitemap is fallback/discovery. Server-rendered; JS not required.
- Language: `nl`; official archive reachability was observed, but safe article coverage is not claimed.
- Evidence: official park, news archive and sitemap URLs above. Checked `2026-08-14`.

### `mondo-verde` — Mondo Verde

- Roster: exact name `Mondo Verde`; country `NL`; city `Landgraaf`; tier `Secondary`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.wereldtuinenmondoverde.nl/nl/` / `www.wereldtuinenmondoverde.nl`. The separate `mondoverde.nl` domain is a plant company and is not used.
- Official source URLs: [Dutch news archive](https://www.wereldtuinenmondoverde.nl/nl/nieuws/), [Dutch RSS](https://www.wereldtuinenmondoverde.nl/nl/feed/), [WordPress sitemap index](https://www.wereldtuinenmondoverde.nl/wp-sitemap.xml), [Dutch post sitemap](https://www.wereldtuinenmondoverde.nl/nl/wp-sitemap-posts-post-1.xml).
- Adapter: `rss` from `/nl/feed/` was observed, but remains disabled because no stable article/news identity pattern was verified for its root-slug items; the Dutch post sitemap is evidence/fallback only. Server-rendered/feed source; JS not required.
- Language: `nl`; official feed reachability was observed, but safe article coverage is not claimed.
- Evidence: official Dutch home, news, feed and sitemap URLs above. Checked `2026-08-14`.

### `brook-valley-zoo` — Brook Valley Zoo

- Roster: exact name `Brook Valley Zoo`; country `NL`; city `Stramproy`; tier `Secondary`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://brookvalleyzoo.com/` / `brookvalleyzoo.com`. Official contact page gives Amentstraat 17 and Savelveld 5, Stramproy.
- Official source URLs: [RSS](https://brookvalleyzoo.com/feed/), [post sitemap](https://brookvalleyzoo.com/post-sitemap.xml), [sitemap index](https://brookvalleyzoo.com/sitemap_index.xml).
- Adapter: `rss` from `/feed/`, with sitemap fallback. The source is
  ready/enabled/recommended after the live content-selector retest.
  Server-rendered/feed source; JS not required.
- Language: `nl`; official RSS discovery returned HTTP 200. The exact
  `.elementor-widget-theme-post-content` selector passed two bounded live runs:
  pass 1 (`a52b0d16b1b04e9ea917d7f888e37008`) stored one clean 1531-character
  article; pass 2 (`6f58334b44ae482e8c1dba05ac7aa655`) found it already known.
  Both runs had no source errors or contamination; `success_claim=true`. Feed
  last build was `2025-10-22`.
- Bounded evidence: `/tmp/zoofan-phase0.9Jx9xK/retest/` artifacts, source
  `sqlite://sources/brook-valley-zoo-news-rss`, checked `2026-08-14`.
- Evidence: official home/contact, feed and sitemap URLs above. Checked
  `2026-08-14`.

### `onze-beestenboel` — Onze Beestenboel

- Roster: exact name `Onze Beestenboel`; country `NL`; city `Venlo`; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.beestenboelvenlo.nl/` / `www.beestenboelvenlo.nl`.
- Official source URLs: [home](https://www.beestenboelvenlo.nl/), [about](https://www.beestenboelvenlo.nl/over-ons/), [news archive](https://www.beestenboelvenlo.nl/category/nieuwsberichten/), [category RSS](https://www.beestenboelvenlo.nl/category/nieuwsberichten/feed/), [sitemap](https://www.beestenboelvenlo.nl/sitemap.xml).
- Adapter: `rss` from the category feed; archive fallback. Server-rendered/feed source; JS not required.
- Language: `nl`; source status `verified_live_stale` (HTTP 200; feed last build `2024-10-27`).
- Evidence: official URLs above. Checked `2026-08-14`.

### `het-dierenrijck` — Het DierenRijck

- Roster: exact name `Het DierenRijck`; country `NL`; city `Reuver`; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://dierenrijck.nl/` / `dierenrijck.nl`.
- Official source URLs: [home](https://dierenrijck.nl/), [about](https://dierenrijck.nl/over-het-dierenrijck/), [new animals archive](https://dierenrijck.nl/category/nieuwedieren/), [category RSS](https://dierenrijck.nl/category/nieuwedieren/feed/), [sitemap](https://dierenrijck.nl/wp-sitemap.xml).
- Adapter: `rss` from the narrow category feed; sitemap fallback. The source
  is ready/enabled/recommended after the live content-selector retest.
  Server-rendered/feed source; JS not required.
- Language: `nl`; official RSS discovery returned HTTP 200. Remediation uses
  the exact selector `#content .detail-content .bizberg_cocntent_wrapper`,
  excludes `.bizberg_post_date`, `.blog-title`, and
  `.bizberg_user_comment_wrapper`, and uses `.bizberg_post_date` with a strict
  Dutch dateline pattern. Pass 1
  (`fad84784b9cd4b1dacccb4b54aaaf113`) stored one clean 866-character article
  dated `17 juli 2024`; pass 2
  (`0ede4f8c597543ec9192e8a700de9d63`) found it already known. Both runs had
  no source errors or contamination; `success_claim=true`.
- Bounded evidence: `/tmp/zoofan-phase0.9Jx9xK/retest/` artifacts, source
  `sqlite://sources/het-dierenrijck-news-rss`, checked `2026-08-14`.
- Evidence: official URLs above. Checked `2026-08-14`.

### `hertenspoor-zoo` — Hertenspoor Zoo

- Roster: exact name `Hertenspoor Zoo`; country `NL`; city `Echt`; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: `http://www.dierenpark-echt.nl/` / `www.dierenpark-echt.nl`. **HTTP is intentional:** the HTTP WordPress site serves the park, while HTTPS currently serves a Webreus placeholder.
- Official source URLs: [news archive](http://www.dierenpark-echt.nl/category/nieuws/), [RSS](http://www.dierenpark-echt.nl/feed/), [sitemap index](http://www.dierenpark-echt.nl/sitemap_index.xml).
- Adapter: `rss` from the HTTP feed, archive fallback. Server-rendered/feed source; JS not required. Keep HTTP and re-check robots/fail-closed behavior before enabling.
- Language: `nl`; source status `verified_live_http_only_stale` (HTTP 200; feed last build `2023-11-20`).
- Evidence: official HTTP home/contact/news/feed/sitemap URLs above. Checked `2026-08-14`.
- Discovery hint only: [third-party directory](https://www.allekinderboerderijen.nl/limburg/echt/dierenpark-kinderboerderij-t-hertenspoor); it is not a crawl source.

### `mini-zoo-venray` — Mini ZOO Venray

- Roster: exact name `Mini ZOO Venray`; country `NL`; city `Venray`; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: none verified. Official news/feed/sitemap/API URLs: none.
- Adapter: `registry_only`; do not invent a domain or use a directory page as a source. Language and JS requirement remain unknown.
- Source status: `no_official_site_verified`; no primary operator or municipal page matching the exact row was found on `2026-08-14`.
- Evidence: none located. Discovery hints only: [Zoopedia record](https://www.zoopedia.org/zoos/mini-zoo/37421/about), [Venray directory guide](https://www.zoopedia.org/guides/europe/netherlands/venray-city/all), [municipal home checked](https://www.venray.nl/). These do not establish an official crawl source.

### `apenheul` — Apenheul

- Roster: exact name `Apenheul`; country `NL`; city `Apeldoorn`; tier `Priority`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.apenheul.nl/` / `www.apenheul.nl`.
- Official source URLs: [news](https://www.apenheul.nl/nieuws), [RSS](https://www.apenheul.nl/rss/), [sitemap](https://www.apenheul.nl/sitemap.xml).
- Adapter: recommended/enabled `rss` source `apenheul-news-rss` from `/rss/`, with sitemap fallback. The broad RSS is narrowed to dated `/nieuws/YYYY/MM/...` links; JS is not required. The exact source-scoped article selector is `#main > section.rte.container`.
- Language: `nl`; source status `verified_live` (HTTP 200; current 2026 items); plan status `ready`, with `success_claim=true`.
- Bounded live evidence: pass 1 `5d7e382bb7544a828c223d8443be7cfd` stored one clean 417-character article; pass 2 `760abc453bd041238efaed5a03d3c348` fetched none and found it already known (`inserted=0`, `updated=0`). Both passes had zero navigation, content, or date errors. RSS candidate date fallback remains preserved (published `2026-07-28T13:19:09+00:00`; article parse status `partial`, quality warnings `0`).
- Evidence artifacts: `/tmp/zoofan-live-retest.c8xlNs/apenheul-pass1.json`, `apenheul-pass2.json`, and matching `.log` files. Checked `2026-08-14`.

### `hof-van-eckberge` — Hof van Eckberge

- Roster: exact name `Hof van Eckberge`; country `NL`; city `Eibergen`; tier `Secondary`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.hofvaneckberge.nl/` / `www.hofvaneckberge.nl`.
- Official source URLs: [blogs archive](https://www.hofvaneckberge.nl/nieuws-uit-het-park), [scoped Joomla RSS](https://www.hofvaneckberge.nl/nieuws-uit-het-park?format=feed&type=rss).
- Adapter: `rss` from the scoped `format=feed&type=rss` URL; archive fallback. The site-wide Joomla feed returns a 404 XML error, so retain the menu-item path. JS not required for the feed/archive.
- Language: `nl`; source status `verified_live` (HTTP 200; current blog cards and RSS entries).
- Evidence: official home/blog/RSS URLs above. Checked `2026-08-14`.

### `uilen-en-dierenpark-de-paay` — Uilen- en Dierenpark De Paay

- Roster: exact name `Uilen- en Dierenpark De Paay`; country `NL`; city `Beesd`; tier `Secondary`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://depaay.nl/` / `depaay.nl`.
- Official source URLs: [news page](https://depaay.nl/nieuws/), [news RSS](https://depaay.nl/category/nieuws/feed/), [sitemap](https://depaay.nl/wp-sitemap.xml).
- Adapter: `rss` from the news category feed, with sitemap fallback. Server-rendered/feed source; JS not required.
- Language: `nl`; source status `verified_live_stale` (HTTP 200; feed last build `2023-09-23`, latest visible posts from 2021).
- Evidence: official home/news/feed/sitemap URLs above. Checked `2026-08-14`.

### `dierenpark-t-goor` — Dierenpark 't Goor

- Roster: exact name `Dierenpark 't Goor`; country `NL`; city `Brummen`; tier `Secondary`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.dierenparkhetgoor.nl/` / `www.dierenparkhetgoor.nl`.
- Official source URLs: no feed, sitemap, or news endpoint was verified. Official identity pages are [home](https://www.dierenparkhetgoor.nl/), [contact](https://www.dierenparkhetgoor.nl/contact/), [Dieren en het Park](https://www.dierenparkhetgoor.nl/dieren-en-het-park/), and [ANBI/stichting](https://www.dierenparkhetgoor.nl/anbi-en-stichting/).
- Adapter: `registry_only` until the official host passes direct fetch/robots checks. The indexed pages are server-rendered; JS is not the blocker.
- Language: `nl`; source status `official_site_indexed_but_fetch_blocked_403`; direct HTTPS check returned HTTP 403 from Cloudflare.
- Evidence: official pages above and [official Brummen municipal PDF](https://www.brummen.nl/fileadmin/brummen/Documenten/Gemeentethuis/GemeenteThuis_week_41_2020.pdf), which describes the foundation and park. Checked `2026-08-14`.
- Discovery hint only: [OSM-derived listing](https://openstreetmap.app/poi/Nederland/Gelderland/Brummen/Troelstralaan/W/59414453); not a source.

### `kleindierwandelpark-hedel` — Kleindierwandelpark Hedel

- Roster: exact name `Kleindierwandelpark Hedel`; country `NL`; city `Hedel`; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: none verified. Official news/feed/sitemap/API URLs: none.
- Adapter: `registry_only`; the official municipality should remain the identity source and Facebook must not be used as a hidden crawler fallback. Language is Dutch; JS/source availability is unknown.
- Source status: `municipal_identity_only_no_site`.
- Evidence: [Maasdriel Kasteel Hedel project page](https://www.maasdriel.nl/inwoner-en-ondernemer/sport-cultuur-en-recreatie/kasteel-hedel) names Stichting Kleindierwandelpark Hedel; [municipal public-green page](https://www.maasdriel.nl/inwoner-en-ondernemer/openbare-ruimte-en-verkeer/onderhoud-openbaar-groen) places the dierenpark in Hedel. Checked `2026-08-14`.
- Discovery hints only: [social mirror](https://www.govserv.org/NL/Hedel/199550780073132/Stichting-Kleindierwandelpark-Hedel), [claimed Facebook page](https://www.facebook.com/DierenparkHedel). Neither is an official crawl source for this project.

### `taman-indonesia` — Taman Indonesia

- Roster: exact name `Taman Indonesia`; country `NL`; city `Kallenkote`; tier `Secondary`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.taman-indonesia.nl/` / `www.taman-indonesia.nl`.
- Official source URLs: [news page](https://www.taman-indonesia.nl/Nieuws), [dedicated news RSS](https://www.taman-indonesia.nl/taman-indonesia-Nieuws.rss), [sitemap](https://www.taman-indonesia.nl/sitemap.xml).
- Adapter: `rss` from the dedicated Nieuws RSS. The site also exposes an Agenda RSS, but that is not an article source and should stay separate. Sitemap is fallback. Static/server-rendered; JS not required.
- Language: `nl`; source status `verified_live` (HTTP 200; official page gives Kallenkote 53 and current 2026 news/events).
- Evidence: official home/Dierenpark/news/RSS/sitemap URLs above. Checked `2026-08-14`.

### `bird-park-de-lorkeershoeve` — Bird Park De Lorkeershoeve

- Roster: exact name `Bird Park De Lorkeershoeve`; country `NL`; **city remains blank**; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.vogelparkdelorkeershoeve.nl/` / `www.vogelparkdelorkeershoeve.nl`.
- Official source URL: [static nieuws archive](https://www.vogelparkdelorkeershoeve.nl/nieuws.html). No RSS, sitemap, or API was exposed.
- Location verification (not a roster overwrite): the official footer gives De Kerkdijk 26, 7775 RA **Lutten**, gemeente Hardenberg.
- Adapter: `archive` from `nieuws.html`; conservative static HTML selectors. JS not required.
- Language: `nl`; source status `verified_live_stale_static` (HTTP 200; official page reachable, visible news includes a season-2020 note).
- Evidence: official home/index/news URLs above. Checked `2026-08-14`.

### `ten-kates-dierenpark` — Ten Kate's Dierenpark

- Roster: exact name `Ten Kate's Dierenpark`; country `NL`; **city remains blank**; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: `https://www.dierenparktenkate.nl/` / `www.dierenparktenkate.nl`.
- Official source URLs: [home](https://www.dierenparktenkate.nl/), [contact](https://www.dierenparktenkate.nl/contact/), [RSS](https://www.dierenparktenkate.nl/feed/), [sitemap](https://www.dierenparktenkate.nl/wp-sitemap.xml), [page sitemap](https://www.dierenparktenkate.nl/wp-sitemap-posts-page-1.xml).
- Location verification (not a roster overwrite): the official site gives Westerveen 42, **Nieuwleusen** (municipality Dalfsen).
- Adapter: `rss` for change detection; the feed is valid but empty/stale (last build 2023-01-24), so do not treat the static page sitemap as news without filtering. JS not required.
- Language: `nl`; source status `verified_live_stale_feed` (HTTP 200).
- Evidence: official home/contact/feed/sitemap URLs above. Checked `2026-08-14`.
- Discovery hint only: [third-party listing](https://www.zoospedia.com/en/i/3926-ten-kates-dierenpark); it is not a source.

### `dierenpark-de-bontekoe` — Dierenpark De Bontekoe

- Roster: exact name `Dierenpark De Bontekoe`; country `NL`; **city remains blank**; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: none verified. Official news/feed/sitemap/API URLs: none.
- Location/identity: official Borne documents refer to a `Stichting Beheer Dierenpark Borne`; Borne is likely the location, but the exact De Bontekoe label is not unambiguously tied to those records. Keep both the blank roster city and the ambiguity.
- Adapter: `registry_only`; do not invent a domain or promote a directory/social page to a source. Language is Dutch; JS/source availability unknown.
- Source status: `official_identity_ambiguous_no_site`.
- Evidence: official [Borne 2021 annual accounts](https://www.borne.nl/file/jaarstukken-gemeente-borne-2021pdf) and [Borne management-quality plan](https://www.borne.nl/file/beheerkwaliteitsplan2018-2023pdf), both referring to Dierenpark Borne but not publishing a De Bontekoe feed/site. Checked `2026-08-14`.
- Discovery hints only: [KVK/company profile mirror](https://companyinfo.nl/organisatieprofiel/activiteiten-van-steunfondsen-niet-op-het-gebied-van-welzijnszorg/stichting-onderhoud-dierenpark-de-bontekoe-borne-50243667-502436670000), [Zoopedia record](https://www.zoopedia.org/zoos/dierenpark-de-bontekoe/37424/about), and [local Borne article](https://www.borneboeit.nl/140132/lintjesregen-2023). These corroborate a likely Borne identity but are not crawl sources.

### `mini-animal-park-deventer` — Mini animal park Deventer

- Roster: exact name `Mini animal park Deventer`; country `NL`; city `Deventer`; tier `Directory`; groups `regional_lab`, `dutch_border`.
- Official website/host: none verified. Official news/feed/sitemap/API URLs: none.
- Adapter: `registry_only`; the municipal document is an identity/location hint, not an article source. Language is Dutch; JS/source availability unknown.
- Source status: `municipal_label_only_no_site`.
- Evidence: the official [Deventer Speelruimteplan (August 2023)](https://www.deventer.nl/fileadmin/deventer_website/Sporten-spelen/Speelruimteplan_Deventer_-_augustus_2023.pdf) labels “Mini animal park” on the municipal play-space map. No operator/site/feed was located. Checked `2026-08-14`.
- Discovery hints only: [Zoopedia record](https://www.zoopedia.org/zoos/mini-animal-park/37357/about) and [Zoopedia Deventer guide](https://www.zoopedia.org/guides/europe/netherlands/deventer-city/all). These are not sources.

## Operational gaps and follow-up

- Keep the five `registry_only` records in the roster and do not silently drop them. A first-party contact or municipal confirmation is needed before adding source URLs for `mini-zoo-venray`, `dierenpark-t-goor`, `kleindierwandelpark-hedel`, `dierenpark-de-bontekoe`, or `mini-animal-park-deventer`.
- Keep the three blank-city cells exactly blank. The official evidence suggests Lutten/Hardenberg for Bird Park, Nieuwleusen/Dalfsen for Ten Kate's, and likely Borne for De Bontekoe; these are recorded as verification notes only.
- Treat stale feeds as valid but low-frequency sources and keep freshness as a
  monitoring note. Brook Valley Zoo (`2025-10-22`) and DierenRijck
  (`2024-08-01`) now have verified live article parsing after their bounded
  retests. Other stale feeds remain: Onze Beestenboel (`2024-10-27`),
  Hertenspoor (`2023-11-20`), De Paay (`2023-09-23`), and Ten Kate's
  (`2023-01-24`, empty). Do not infer closure solely from feed staleness.
- Hertenspoor is HTTP-only in the current check. Preserve the exact HTTP source URLs and run the crawler's normal robots/fail-closed checks before enabling it.
- Dierenpark 't Goor's official site was discoverable and identity-confirming but returned Cloudflare 403 to direct requests. Keep it `registry_only` until an allowed, deterministic transport can verify robots and source behavior.
