# Regional Lab — Dutch Border adapter plans

Checked `2026-08-14`. These are conservative source plans for every one of the
18 `Dutch Border` rows in
`data/zoofan_phase0_zoo_list_v0_1.md`. The machine-readable plan is
[`regional-dutch-adapters.json`](regional-dutch-adapters.json).

The complete authoritative roster file SHA-256 is
`6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd` (92
LF-terminated lines, 5220 bytes). All rows retain the exact roster name,
city (including three explicit `null` city cells), tier, `country_code=NL`, and groups
`regional_lab` + `dutch_border`. The stable ID `gaiazoo` is preserved.

## Safety and count conventions

- Only official zoo/operator or official municipal/institution hosts are
  source plans. Third-party pages remain discovery hints only.
- `allow_regex` and `exclude_regex` are URL-policy patterns, not parser
  guesses. Every executable source is bounded with `max_pages=1`; no plan
  exceeds two pages. Robots and redirects must still be checked fail-closed
  at runtime.
- `parsed_candidate_count` is the direct parser count before URL policy;
  `accepted_candidate_count` is the unique count after the listed
  allow/exclude rules. These are observations at check time.
- Probe-only root-slug counts are evidence and are excluded from the primary
  accepted-candidate total; they never authorize a crawler source.
- Primary source records: 16 (9 enabled, 7 disabled); Gaia's two preserved
  health registry records are listed as fallbacks, and its root-slug probe is
  kept separately as disabled evidence.
- Direct observations total 505 parsed candidates and 120 candidates accepted
  under the safe primary-source policies; 111 accepted candidates belong to
  currently enabled plans, including the 4 candidates whose content selectors
  passed live retest. The 20 Gaia/Mondo
  root-slug observations and 9 HTTP-only Hertenspoor observations are
  disabled-plan observations. Gaia's
  preserved RSS emitted 10 root-slug items and accepted 0 under `/nieuws/<slug>`.
  Kasteelpark Born has 144 cards, but zero safe generic-parser candidates until
  its malformed relative links are rewritten deterministically.

| Stable ID | Exact roster name | City | Tier | Primary source ID(s) | Plan status | Direct observation |
| --- | --- | --- | --- | --- | --- | ---: |
| `gaiazoo` | GaiaZOO | Kerkrade | Priority | `gaiazoo-news-rss`; `gaiazoo-news-archive` | ready (preserved `/nieuws/<slug>` rule) | RSS 10 / 0; archive not re-probed |
| `kasteelpark-born` | Kasteelpark Born | Born | Secondary | `kasteelpark-born-news-archive` | unsupported pending URL rewrite | 144 / 0 |
| `mondo-verde` | Mondo Verde | Landgraaf | Secondary | `mondo-verde-news-rss` | unsupported (root-slug policy) | 10 / 10 probe-only |
| `brook-valley-zoo` | Brook Valley Zoo | Stramproy | Secondary | `brook-valley-zoo-news-rss` | ready (recommended, verified) | 1 / 1 discovery; 1 stored |
| `onze-beestenboel` | Onze Beestenboel | Venlo | Directory | `onze-beestenboel-news-rss` | ready_stale | 10 / 10 |
| `het-dierenrijck` | Het DierenRijck | Reuver | Directory | `het-dierenrijck-news-rss` | ready (recommended, verified) | 2 / 2 discovery; 1 stored |
| `hertenspoor-zoo` | Hertenspoor Zoo | Echt | Directory | `hertenspoor-news-rss` | ready_http_only | 10 / 9 |
| `mini-zoo-venray` | Mini ZOO Venray | Venray | Directory | — | source_not_found | — |
| `apenheul` | Apenheul | Apeldoorn | Priority | `apenheul-news-rss` | ready (recommended, verified) | 280 / 60; 1 stored |
| `hof-van-eckberge` | Hof van Eckberge | Eibergen | Secondary | `hof-van-eckberge-news-rss` | ready | 20 / 20 |
| `uilen-en-dierenpark-de-paay` | Uilen- en Dierenpark De Paay | Beesd | Secondary | `de-paay-news-rss` | ready_stale | 2 / 2 |
| `dierenpark-t-goor` | Dierenpark 't Goor | Brummen | Secondary | `dierenpark-t-goor-registry` | registry_only | HTTP 403 |
| `kleindierwandelpark-hedel` | Kleindierwandelpark Hedel | Hedel | Directory | — (evidence only) | registry_only | identity evidence only |
| `taman-indonesia` | Taman Indonesia | Kallenkote | Secondary | `taman-indonesia-news-archive`; `taman-indonesia-news-rss-unsupported` | ready_archive_rss_unverified | 16 / 16; RSS 0 |
| `bird-park-de-lorkeershoeve` | Bird Park De Lorkeershoeve | `null` (roster blank) | Directory | `bird-park-de-lorkeershoeve-news-registry` | registry_only | 0 / 0 |
| `ten-kates-dierenpark` | Ten Kate's Dierenpark | `null` (roster blank) | Directory | `ten-kates-dierenpark-news-rss` | unsupported | 0 / 0 |
| `dierenpark-de-bontekoe` | Dierenpark De Bontekoe | `null` (roster blank) | Directory | — (evidence only) | registry_only | identity evidence only |
| `mini-animal-park-deventer` | Mini animal park Deventer | Deventer | Directory | — | source_not_found | — |

The five explicit roster gaps are `mini-zoo-venray`, `dierenpark-t-goor`,
`kleindierwandelpark-hedel`, `dierenpark-de-bontekoe`, and
`mini-animal-park-deventer`. They are retained with `source_not_found` or
`registry_only`; no URL was invented.

## Executable RSS plans

All RSS plans use `max_pages=1`. RSS has no CSS card selectors; the exact
URL-policy patterns below are the executable narrowing contract.

- **`gaiazoo-news-rss`** — `rss`, preserved and enabled,
  `https://www.gaiazoo.nl/category/nieuws/feed/`, official host
  `www.gaiazoo.nl`.
  Exact current rule:
  `allow_regex=^https://www\.gaiazoo\.nl/nieuws/[^/?#]+/?$`, with the current
  article-content selectors and navigation patterns preserved. Direct `200
  application/rss+xml`: 10 parsed, 0 accepted, 0 unique because the observed
  feed items are root slugs. This is not a live-success claim; it records the
  safe policy's current result.
- **`gaiazoo-news-archive`** — `archive`, preserved and enabled,
  `https://www.gaiazoo.nl/nieuws-vlogs/`, official host `www.gaiazoo.nl`.
  Exact current selectors are `article.card`, `a[href]`, `.card-title`, and
  `.date`, with `max_pages=2`, article-content selectors, and the same anchored
  `/nieuws/<slug>` allow rule. No new live candidate count is asserted.
- **`mondo-verde-news-rss`** — `rss`, observed but disabled,
  `https://www.wereldtuinenmondoverde.nl/nl/feed/`, official host
  `www.wereldtuinenmondoverde.nl`.
  The former broad `/nl/<slug>` allow rule is removed because no stable
  article/news identity pattern was verified. The disabled primary plan
  accepts 0 of 10 parsed items; its distinct root-slug probe observed 10 / 10
  only as historical evidence. The separate `mondoverde.nl` plant company is
  not authorized.
- **`brook-valley-zoo-news-rss`** — `rss`, ready/enabled/recommended,
  `https://brookvalleyzoo.com/feed/`, host `brookvalleyzoo.com`.
  `allow_regex=^https://brookvalleyzoo\.com/nieuws/[^/?#]+/?$`;
  exclude `/nieuws/(feed|category|tag)`. Discovery was HTTP 200 with 1 / 1 / 1
  candidate. The exact remediation selector
  `.elementor-widget-theme-post-content` passed two bounded live runs: pass 1
  (`a52b0d16b1b04e9ea917d7f888e37008`) stored one clean 1531-character article;
  pass 2 (`6f58334b44ae482e8c1dba05ac7aa655`) found it already known. Both runs
  completed without source errors or contamination; `success_claim=true`.
  Evidence: `/tmp/zoofan-phase0.9Jx9xK/retest/` artifacts, checked
  `2026-08-14`. Feed last build was 2025-10-22, so freshness remains a
  monitoring note rather than a failure.
- **`onze-beestenboel-news-rss`** — `rss`,
  `https://www.beestenboelvenlo.nl/category/nieuwsberichten/feed/`, host
  `www.beestenboelvenlo.nl`.
  `allow_regex=^https://www\.beestenboelvenlo\.nl/nieuwsberichten/[^/?#]+/?$`;
  no additional exclude pattern. Direct `200`: 10 / 10 / 10. Feed last
  build was 2024-10-27.
- **`het-dierenrijck-news-rss`** — `rss`, ready/enabled/recommended,
  `https://dierenrijck.nl/category/nieuwedieren/feed/`, host `dierenrijck.nl`.
  `allow_regex=^https://dierenrijck\.nl/\d{4}/\d{2}/\d{2}/[^/?#]+/?$`;
  no additional URL exclude pattern. Discovery was HTTP 200 with 2 / 2 / 2
  candidates. The exact content selector
  `#content .detail-content .bizberg_cocntent_wrapper`, exclusions
  `.bizberg_post_date`, `.blog-title`, `.bizberg_user_comment_wrapper`, and
  `.bizberg_post_date` strict Dutch dateline passed two bounded live runs:
  pass 1 (`fad84784b9cd4b1dacccb4b54aaaf113`) stored one clean 866-character
  article dated `17 juli 2024`; pass 2
  (`0ede4f8c597543ec9192e8a700de9d63`) found it already known. Both runs
  completed without source errors or contamination; `success_claim=true`.
  Evidence: `/tmp/zoofan-phase0.9Jx9xK/retest/` artifacts, checked
  `2026-08-14`.
- **`hertenspoor-news-rss`** — `rss`,
  `http://www.dierenpark-echt.nl/feed/`, host `www.dierenpark-echt.nl`.
  `allow_regex=^http://www\.dierenpark-echt\.nl/[^/?#]+/?$`;
  exclude `feed`, `category`, `contactgegevens`, `openingstijden`, `route`,
  `prijzen`, `parkeren`, `privacy`, and `algemene-voorwaarden` paths.
  Direct HTTP `200`: 10 parsed, 9 accepted, 9 unique. Keep disabled until
  HTTP robots/redirect behavior is rechecked; HTTPS serves a placeholder.
- **`apenheul-news-rss`** — `rss`, `https://www.apenheul.nl/rss/`, host
  `www.apenheul.nl`.
  `allow_regex=^https://www\.apenheul\.nl/nieuws/\d{4}/\d{2}/[^/?#]+/?$`.
  Direct `200`: 280 parsed, 60 accepted, 60 unique. The allow rule excludes
  jobs, events, thank-you pages, and other broad RSS entries. Use the exact
  source-scoped article selector `#main > section.rte.container`; retain RSS
  candidate date fallback. The bounded live retest stored one clean 417-character
  article in pass 1 (`5d7e382bb7544a828c223d8443be7cfd`) and found it already
  known in pass 2 (`760abc453bd041238efaed5a03d3c348`, inserted/updated 0), with
  zero navigation/content/date errors. The plan is ready, enabled, recommended,
  and carries `success_claim=true`.
- **`hof-van-eckberge-news-rss`** — `rss`,
  `https://www.hofvaneckberge.nl/nieuws-uit-het-park?format=feed&type=rss`,
  host `www.hofvaneckberge.nl`.
  `allow_regex=^https://www\.hofvaneckberge\.nl/nieuws-uit-het-park/\d+-[^/?#]+/?$`.
  Direct `200`: 20 / 20 / 20. Use this scoped Joomla feed; the site-wide
  query feed returned a 404 XML error.
- **`de-paay-news-rss`** — `rss`,
  `https://depaay.nl/category/nieuws/feed/`, host `depaay.nl`.
  `allow_regex=^https://depaay\.nl/\d{4}/\d{2}/\d{2}/[^/?#]+/?$`.
  Direct `200`: 2 / 2 / 2. The feed is valid but stale (last build 2023;
  latest visible posts 2021).

## Executable archive plans

- **`kasteelpark-born-news-archive`** — `archive`,
  `https://kasteelparkborn.nl/het-park/nieuws/`, host `kasteelparkborn.nl`.
  Exact selectors: `article_selector=li.clearfix`,
  `link_selector=a.button`, `title_selector=.info > strong`,
  `date_selector=.info > span.date`, no pagination, `max_pages=1`.
  Expected allow pattern is
  `^https://kasteelparkborn\.nl/het-park/nieuws/\d{4}/\d{2}/\d{2}/[^/?#]+/?$`.
  The page returned 144 dated cards, but its relative hrefs resolve to a
  duplicated path under the generic parser, so the plan remains disabled:
  144 raw/canonicalizable, 0 safely accepted. A future adapter must rewrite
  `het-park/nieuws/...` against the origin root before URL policy; it must
  not fetch the malformed duplicate URL.
- **`taman-indonesia-news-archive`** — `archive`,
  `https://www.taman-indonesia.nl/Nieuws`, host `www.taman-indonesia.nl`.
  Exact selectors: `article_selector=div#content-content >
  div.agenda-item.bruin`, `link_selector=a[href^="Nieuws/"]`,
  `title_selector=h6`, no date or pagination selector, `max_pages=1`.
  Allow only
  `^https://www\.taman-indonesia\.nl/Nieuws/[^/?#]+/?$`; exclude root
  `Nieuws`, `Agenda`, `Exposities`, `Webshop`, `Bezoekersinformatie`, and
  `Contact` paths. Direct `200 text/html`: 16 / 16 / 16. Article pages must
  supply dates because the cards expose no date element.

The official Taman RSS URL is retained as source ID
`taman-indonesia-news-rss-unsupported`, but direct `200` returned the HTML
site shell rather than XML, yielding zero parsed candidates. It must remain
disabled until restored.

## Registry-only, unsupported, and source-not-found plans

- **`gaiazoo-root-slug-feed-probe`** — probe-only `rss` observation of
  `https://www.gaiazoo.nl/category/nieuws/feed/`, official host
  `www.gaiazoo.nl`, HTTP 200, 10 root-slug candidates. It is explicitly
  `unsupported` and disabled; its broad root-slug shape must never be merged
  into either preserved Gaia source ID. Gaia sitemap and press records remain
  health-only fallbacks, not article sources.
- **`mondo-verde-root-slug-feed-probe`** — probe-only `rss` observation of
  `https://www.wereldtuinenmondoverde.nl/nl/feed/`, official host
  `www.wereldtuinenmondoverde.nl`, HTTP 200, 10 root-slug candidates. It is
  explicitly `unsupported` and disabled; the broad `/nl/<slug>` shape must not
  be promoted to `mondo-verde-news-rss`.

- **`dierenpark-t-goor-registry`** — `registry_only`,
  `https://www.dierenparkhetgoor.nl/`, host `www.dierenparkhetgoor.nl`.
  Indexed official identity pages are known, but direct research returned
  Cloudflare HTTP 403 and no official article/feed/sitemap endpoint was
  verified. No candidate count is claimed.
- **`kleindierwandelpark-hedel`** — no source record,
  municipal URLs remain evidence metadata only. The identity evidence page is
  `https://www.maasdriel.nl/inwoner-en-ondernemer/sport-cultuur-en-recreatie/kasteel-hedel`
  (host `www.maasdriel.nl`, HTTP 200); no standalone official source was found.
- **`bird-park-de-lorkeershoeve-news-registry`** — `registry_only`,
  `https://www.vogelparkdelorkeershoeve.nl/nieuws.html`, host
  `www.vogelparkdelorkeershoeve.nl`, HTTP 200, 0 article links. Keep the
  roster city blank; the static page points visitors to Facebook for updates.
- **`ten-kates-dierenpark-news-rss`** — `unsupported`,
  `https://www.dierenparktenkate.nl/feed/`, host
  `www.dierenparktenkate.nl`, HTTP 200 RSS but 0 items. The feed was last
  built 2023-01-24; the page sitemap is not promoted to a news source.
- **`dierenpark-de-bontekoe`** — no source record, official Borne URLs remain
  evidence metadata only. The record
  `https://www.borne.nl/file/jaarstukken-gemeente-borne-2021pdf` (host
  `www.borne.nl`, currently HTTP 403) mentions Dierenpark Borne but does not
  unambiguously establish the exact De Bontekoe label or operator. Keep the
  roster city null.
- **`mini-zoo-venray`** — `source_not_found`, no official URL verified.
- **`mini-animal-park-deventer`** — `source_not_found`, no operator or
  standalone official source verified. The official municipal map PDF is
  identity/location evidence only and is not a source plan.

Disabled discovery-only sitemap records (with official hosts) are included
in the JSON for Kasteelpark Born, Mondo Verde, Brook Valley Zoo,
Onze Beestenboel, Het DierenRijck, Hertenspoor, Apenheul, De Paay, Taman
Indonesia, and Ten Kate's Dierenpark. GaiaZOO's sitemap and press records are
preserved health-only fallbacks and are enabled only as registry checks; they
are not article sources. No cross-domain or browser fallback is proposed.
