# Regional Lab NRW — conservative source adapter plans

Checked: **2026-08-14**  
Authoritative roster: [`data/zoofan_phase0_zoo_list_v0_1.md`](../../data/zoofan_phase0_zoo_list_v0_1.md)  
Roster SHA-256: `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`

The machine-readable, executable plan is [`regional-nrw-adapters.json`](regional-nrw-adapters.json). It preserves all 25 rows under `Region=NRW` and the required groups `regional_lab` and `nrw`.

## Scope and counts

| Measure | Count |
| --- | ---: |
| Roster rows / adapter plans | 25 / 25 |
| Original research status (`success` / `partial` / `blocked`) | 20 / 4 / 1 |
| `source_status=ready` | 5 |
| `source_status=registry_only` | 15 |
| `source_status=unsupported` | 5 |
| Positive candidate rows | 5 |
| Gap rows | 20 |
| Canonical `archive` / `rss` / `registry_only` / `unsupported` | 5 / 0 / 15 / 5 |

`ready` means a robots-readable HTTP 200 response was parsed with the built-in adapter and produced accepted official candidates. `registry_only` retains an official identity or endpoint for health/registration but deliberately emits no article candidates. `unsupported` means the built-in parser contract cannot safely consume the observed surface without a custom or browser/JS adapter. Every plan has `max_pages <= 2`; registry and unsupported plans have empty selector/allow/exclude settings rather than speculative rules.

## Safety and verification rules

- Checks used the repository `Fetcher` and `DiscoveryEngine` with robots checking enabled and fail-closed. No Playwright, JavaScript bypass, robots override, or third-party source was used.
- Ready archive plans use exact server-rendered CSS selectors. The three reviewed RSS feeds are parseable, but are disabled because their observed links have no safely narrow article/news path.
- `allow_regex` values are full-URL, official-host, narrow path rules. Explicit `exclude_regex` values exclude source/feed roots; URLPolicy still applies its own official-host, source-root, and navigation checks.
- Candidate counts are the direct check results on 2026-08-14 and can change as official pages change. Evidence URLs and detailed reasons for every row are in the JSON.

## All 25 plans

| Stable ID | Source ID | Canonical kind | Status | Official source URL | Candidate result |
| --- | --- | --- | --- | --- | ---: |
| `zoo-duisburg` | `zoo-duisburg-news` | `registry_only` | registry_only | [Aktuelles](https://zoo-duisburg.de/unser-zoo/aktuelles) | 0 — robots HTTP 404 |
| `zoo-krefeld` | `zoo-krefeld-news` | `registry_only` | registry_only | [Aktuelles](https://www.zookrefeld.de/aktuelles) | 0 — robots HTTP 404 |
| `gruener-zoo-wuppertal` | `gruener-zoo-wuppertal-news` | `unsupported` | unsupported | [municipal Aktuelles](https://www.wuppertal.de/microsite/zoo/Aktuelles/aktuelles.php) | 0 — Cloudflare challenge |
| `zoom-erlebniswelt` | `zoom-erlebniswelt-press` | `archive` | ready | [Presse](https://www.zoom-erlebniswelt.de/presse/) | 6 / 6 accepted |
| `zoo-dortmund` | `zoo-dortmund-news` | `registry_only` | registry_only | [municipal Zoo page](https://www.dortmund.de/themen/freizeit-und-kultur/parks-seen-und-zoo/zoo-dortmund/) | 0 — robots redirect loop |
| `allwetterzoo-muenster` | `allwetterzoo-muenster-news` | `unsupported` | unsupported | [News](https://www.allwetterzoo.de/de/ueber-den-zoo/news/) | 0 — `data-href`, no `a[href]` |
| `aachener-tierpark-euregiozoo` | `aachener-tierpark-euregiozoo-news-rss` | `registry_only` | registry_only | [RSS](https://www.euregiozoo.de/de/kontakt/rss.php) | 0 — robots HTTP 404 |
| `tierpark-fossilium-bochum` | `tierpark-fossilium-bochum-news` | `archive` | ready | [Aktuelles](https://www.tierpark-bochum.de/neuigkeiten/aktuelles) | 50 / 50 accepted |
| `aquazoo-loebbecke-museum` | `aquazoo-loebbecke-museum-news` | `archive` | ready | [Aktuelles](https://aquazoo-duesseldorf.de/aktuelles) | 18 / 18 accepted |
| `naturzoo-rheine` | `naturzoo-rheine-news` | `archive` | ready | [Aktuelles](https://www.naturzoo.de/aktuelles/) | 18 / 18 accepted across 2 pages |
| `tiergarten-kleve` | `tiergarten-kleve-news-rss` | `unsupported` | unsupported | [RSS](https://tiergarten-kleve.de/feed/) | 10 parsed, 0 active — root slugs only |
| `tiergarten-moenchengladbach` | `tiergarten-moenchengladbach-news-rss` | `unsupported` | unsupported | [RSS](https://www.tiergarten-moenchengladbach.de/feed/) | 10 parsed, 0 active — root slugs only |
| `terrazoo-rheinberg` | `terrazoo-rheinberg-news-rss` | `registry_only` | registry_only | [RSS](https://www.terrazoo.de/?format=feed&type=rss) | 0 — robots HTTP 404 |
| `sea-life-oberhausen` | `sea-life-oberhausen-press` | `registry_only` | registry_only | [Press](https://www.visitsealife.com/oberhausen/informationen/presse/) | 0 — no dedicated contract verified |
| `tiergehege-kaisergarten` | `tiergehege-kaisergarten-news-rss` | `registry_only` | registry_only | [RSS](https://www.tiergehege-kaisergarten.de/tg/feed/) | 0 — redirects to `/tg/` |
| `heimat-tierpark-olderdissen` | `heimat-tierpark-olderdissen-news` | `registry_only` | registry_only | [municipal Tierpark](https://www.bielefeld.de/tierpark) | 0 — malformed robots |
| `safariland-stukenbrock` | `safariland-stukenbrock-press` | `registry_only` | registry_only | [Press](https://safariland-stukenbrock.de/infos/presse.html) | 0 — robots HTTP 404 |
| `affen-und-vogelpark-eckenhagen` | `affen-und-vogelpark-eckenhagen-news-rss` | `registry_only` | registry_only | [RSS](https://affen-und-vogelpark.de/feed/) | 1 feed item, 0 usable |
| `wildpark-grafenberg` | `wildpark-grafenberg-registry` | `registry_only` | registry_only | [municipal Wildpark](https://www.duesseldorf.de/stadtgruen/wald/wildpark) | 0 — no Wildpark-specific feed |
| `biotopwildpark-anholter-schweiz` | `biotopwildpark-anholter-schweiz-registry` | `registry_only` | registry_only | [canonical feed](https://www.wildpark-nrw.de/feed/) | 0 — host migration not promoted |
| `naturwildpark-granat` | `naturwildpark-granat-news-rss` | `unsupported` | unsupported | [RSS](https://www.naturwildpark.de/feed/) | 6 parsed, 0 active — root slugs only, stale |
| `wildwald-vosswinkel` | `wildwald-vosswinkel-news` | `registry_only` | registry_only | [Aktuelles](https://wildwald.de/aktuelles/) | 0 — malformed robots after redirect |
| `hochwildpark-rheinland` | `hochwildpark-rheinland-news-rss` | `registry_only` | registry_only | [RSS](https://www.hochwildpark-rheinland.de/feed/) | 0 — empty feed |
| `zoo-im-brueckenkopfpark` | `zoo-im-brueckenkopfpark-registry` | `registry_only` | registry_only | [Events](https://brueckenkopf-park.de/events/) | 0 — events-only surface |
| `adlerwarte-berlebeck` | `adlerwarte-berlebeck-news` | `archive` | ready | [Aktuelles](https://www.detmold-adlerwarte.de/aktuelles/) | 12 / 12 accepted |

## Executable ready plans

These are the exact `adapter_proposal.config` objects in the JSON. The source IDs and URLs are stable; use the JSON as the configuration-shaped record.

### ZOOM Erlebniswelt

```json
{
  "article_selector": "div.t-entry",
  "link_selector": "a[href*=\"/presse-2026/\"]",
  "title_selector": "h3.t-entry-title",
  "date_selector": "span.t-entry-date",
  "allow_regex": ["^https://www\\.zoom-erlebniswelt\\.de/presse-2026/[^/?#]+/?$"],
  "exclude_regex": ["^https://www\\.zoom-erlebniswelt\\.de/(?:presse/?|tierische-neuigkeiten/?|newsletter/?|kontakt/?|impressum/?)$"],
  "max_pages": 1
}
```

The direct page contained 24 `div.t-entry` cards; 6 press-detail links matched the allow rule. The separate `/tierische-neuigkeiten/` page remains first-party evidence but is not combined into this source ID.

### Tierpark + Fossilium Bochum

```json
{
  "article_selector": "div.news",
  "link_selector": "div.more a[href*=\"/neuigkeiten/aktuelles/detail/\"]",
  "title_selector": "h4",
  "date_selector": "span.date",
  "article_content_selector": "main .ais-news-details > .col-md-12",
  "allow_regex": ["^https://www\\.tierpark-bochum\\.de/neuigkeiten/aktuelles/detail/[^/?#]+/?$"],
  "exclude_regex": ["^https://www\\.tierpark-bochum\\.de/neuigkeiten/aktuelles/?$"],
  "max_pages": 1
}
```

The direct page contained 50 `div.news` cards. Dates are visible as German numeric `dd.mm.yyyy` text; the selector is retained and the raw date evidence is preserved even when the current localized date parser does not turn that numeric form into a timestamp. The exact article body selector is `main .ais-news-details > .col-md-12` and is quoted in the YAML fragment for cross-format consistency.

The final quality remediation was exercised in two bounded live runs on
2026-08-14. Pass 1 (`0c2ed2d8d0954098a18230cffb1b7147`) completed with HTTP
200, discovered 50, fetched/parsed/stored 1 article, inserted 1 and updated
0. The stored official article was
[`Lernen, staunen, entdecken`](https://www.tierpark-bochum.de/neuigkeiten/aktuelles/detail/2026-08-bk-2026),
published `2026-08-11T00:00:00+00:00`, with 3271 content characters and zero
quality or parser warnings. Pass 2
(`9b35f084a4094bee8d694b4f1ad287fa`) completed with discovered 50,
fetched/parsed/stored 0, inserted 0, updated 0, and one already-known
article. Navigation, content, content-parse, date, and parser warnings were
all zero; the plan remains `ready`, `recommended`, and a successful
live-verified claim. The JSON carries the four local run/log evidence paths
and the same source ID.

### Aquazoo Löbbecke Museum

```json
{
  "article_selector": "div.col-12.col-md-6.col-xl-4",
  "link_selector": "a[href*=\"/aktuelles/\"]",
  "title_selector": "a.mb-2",
  "date_selector": "div.tile > div > p",
  "allow_regex": ["^https://aquazoo-duesseldorf\\.de/aktuelles/[^/?#]+/?$"],
  "exclude_regex": ["^https://aquazoo-duesseldorf\\.de/aktuelles/?$"],
  "max_pages": 1
}
```

The direct page contained 18 matching news tiles. Dates are visible as German numeric `dd.mm.yyyy` text and are retained as raw evidence.

### NaturZoo Rheine

```json
{
  "article_selector": "div.row.news-list-item",
  "link_selector": "a[href*=\"/aktuelles/artikel/\"]",
  "title_selector": "h3[itemprop=\"headline\"]",
  "date_selector": "time[itemprop=\"datePublished\"]",
  "article_content_selector": "#main > .content .news-single .news-text-wrap",
  "pagination_selector": "a[href*=\"tx_news_pi1\"][href*=\"currentPage\"]",
  "allow_regex": ["^https://www\\.naturzoo\\.de/aktuelles/artikel/[^/?#]+/?$"],
  "exclude_regex": ["^https://www\\.naturzoo\\.de/aktuelles/?$"],
  "max_pages": 2
}
```

The first page contained 10 news cards and the bounded pagination check yielded 18 accepted article links across two pages. Dates use `time[itemprop=datePublished]` with ISO `datetime` values. The exact article body selector is `#main > .content .news-single .news-text-wrap`; it is quoted in the YAML fragment because an unquoted leading `#` would be parsed as a comment.

The final quality remediation was exercised in two bounded live runs on
2026-08-14. Pass 1 (`67f3b8d49d1b4bf79b9965cab0e93764`) completed with HTTP
200, discovered 10, fetched/parsed/stored 1 article, inserted 1 and updated
0. The stored official article was
[`Bunt, giftig und neu`](https://www.naturzoo.de/aktuelles/artikel/bunt-giftig-und-neu),
published `2026-05-29T10:55:00+02:00`, with 661 content characters and zero
quality warnings. Its content had no contact, directions, opening-hours,
download, or back-navigation contamination. Pass 2
(`99415d6aabbb4206951fac24dd3ee51a`) completed with discovered 10,
fetched/parsed/stored 0, inserted 0, updated 0, and one already-known
article. Navigation, content, content-parse, and date errors were all zero;
the plan remains `ready`, `recommended`, and a successful live-verified
claim. The JSON carries the four local run/log evidence paths and the same
source ID.

### Reviewed but disabled RSS evidence

Kleve, Mönchengladbach, and Granat each returned a robots-readable HTTP 200
feed and `parse_feed` samples (10, 10, and 6 items respectively). Every
observed article URL is a generic one-segment root slug, with no evidenced
`/news/`, `/artikel/`, category, or post-type path. A generic root-slug
allow-regex would be too broad for an active source, so all three plans now
use `canonical_kind=unsupported`, empty allow/exclude rules, and zero active
candidates. Granat’s latest observed item is additionally stale (2018-11-26).

### Adlerwarte Berlebeck

```json
{
  "article_selector": "div.card.card--linked",
  "link_selector": "a[href*=\"/news/\"]",
  "title_selector": ".card-title [itemprop=\"headline\"]",
  "date_selector": "time[itemprop=\"datePublished\"]",
  "allow_regex": ["^https://www\\.detmold-adlerwarte\\.de/news/[^/?#]+/?$"],
  "exclude_regex": ["^https://www\\.detmold-adlerwarte\\.de/aktuelles/?$"],
  "max_pages": 1
}
```

The direct page contained 12 `div.card.card--linked` Article cards, all yielding accepted `/news/` candidates. The official sitemap is retained as evidence but is not needed by this bounded archive plan.

## Explicit gaps retained in the plan

- Zoo Duisburg, Zoo Krefeld, Euregiozoo, TerraZoo, and Safariland have official source URLs but robots HTTP 404; they remain `registry_only` under fail-closed fetching.
- Zoo Dortmund’s municipal robots request exceeded the redirect limit; it remains `registry_only`.
- Grüner Zoo Wuppertal remains `unsupported` because direct HTTP encountered the official Cloudflare challenge; no browser fallback is assumed.
- Allwetterzoo Münster is `unsupported` because its 12 visible cards expose `data-href` on `div.news-teaser-link`, which the generic archive parser cannot read as an article link.
- SEA LIFE Oberhausen, Wildpark Grafenberg, and Zoo im Brückenkopfpark retain official identity/press/events evidence but no dedicated article discovery contract; they are `registry_only`.
- Tiergarten Kleve, Tiergarten Mönchengladbach, and Naturwildpark Granat have parseable feeds but only generic root slugs; they are explicitly `unsupported` until a narrower official article path is evidenced.
- Tiergehege Kaisergarten’s official feed redirected to `/tg/` and parsed zero items; Eckenhagen’s feed contained only the default “Hallo Welt!” item; Hochwildpark’s feed parsed zero items. All remain `registry_only`.
- Olderdissen and Wildwald have malformed robots responses; Anholter Schweiz has a canonical-host migration. These rows remain present and `registry_only` rather than being silently omitted.

The JSON record contains the exact official evidence URL list, robots/source HTTP result, checked-at timestamp, observed selector/feed evidence, and candidate counts for every row.

## Reviewed German umlaut ID aliases

Stable IDs remain ASCII lowercase because the repository ID contract is URL-safe.
The following display/lookup spellings were reviewed and intentionally do not
create duplicate IDs:

| Stable ID | Reviewed umlaut alias | Chosen ID |
| --- | --- | --- |
| `gruener-zoo-wuppertal` | `grüner-zoo-wuppertal` | `gruener-zoo-wuppertal` |
| `allwetterzoo-muenster` | `allwetterzoo-münster` | `allwetterzoo-muenster` |
| `aquazoo-loebbecke-museum` | `aquazoo-löbbecke-museum` | `aquazoo-loebbecke-museum` |
| `tiergarten-moenchengladbach` | `tiergarten-mönchengladbach` | `tiergarten-moenchengladbach` |
| `zoo-im-brueckenkopfpark` | `zoo-im-brückenkopfpark` | `zoo-im-brueckenkopfpark` |
