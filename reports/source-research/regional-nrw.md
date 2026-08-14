# Regional Lab source research — NRW

**Checked:** 2026-08-14  
**Authoritative roster:** [`data/zoofan_phase0_zoo_list_v0_1.md`](../../data/zoofan_phase0_zoo_list_v0_1.md)  
**Roster SHA-256:** `6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`

## Scope and counts

This report covers every row whose roster region is `NRW` under the
`Regional Lab` heading. Country code is `DE`; every row carries the required
groups `regional_lab` and `nrw`. Official zoo/operator or municipal sites are
the evidence standard. Search results and third-party directories were used
only as discovery hints and are not treated as identity evidence.

| Measure | Count |
| --- | ---: |
| Roster rows | 25 |
| Official identities verified | 25 |
| Rows with a source gap or access caveat | 5 |
| `source_status=success` | 20 |
| `source_status=partial` | 4 |
| `source_status=blocked` | 1 |

`success` means that a first-party source suitable for the proposed adapter
was found. `partial` means the official identity is clear but only an events
or press surface, a municipal identity page, or a canonical-host migration
was found. `blocked` means the official source is known but direct HTTP
research access encountered an access challenge. These classifications are
deliberate and no small facility was omitted.

The machine-readable record with the same evidence is
[`regional-nrw.json`](regional-nrw.json).

## Tierpark + Fossilium Bochum quality retest

The `tierpark-fossilium-bochum-news` archive remains a recommended, enabled
`ready` source on the official `www.tierpark-bochum.de` host. Its article body
selector is exactly `main .ais-news-details > .col-md-12` (quoted in the YAML
fragment). A bounded live retest on 2026-08-14 stored one official article in
pass 1 (`0c2ed2d8d0954098a18230cffb1b7147`):

| Field | Evidence |
| --- | --- |
| Article | [Lernen, staunen, entdecken](https://www.tierpark-bochum.de/neuigkeiten/aktuelles/detail/2026-08-bk-2026) |
| Published | `2026-08-11T00:00:00+00:00` |
| Pass 1 | HTTP 200; discovered 50; fetched/parsed/stored 1; inserted 1; updated 0 |
| Pass 2 (`9b35f084a4094bee8d694b4f1ad287fa`) | discovered 50; fetched/parsed/stored 0; inserted 0; updated 0; already known 1 |
| Content | 3271 characters; parse success; zero quality/parser warnings |
| Error checks | zero navigation, content, content-parse, and date errors |

The JSON and adapter plan retain the dated run artifacts and exact source ID;
no live fetch is part of artifact validation.

## NaturZoo Rheine quality retest

The `naturzoo-rheine-news` archive remains a recommended, enabled `ready`
source on the official `www.naturzoo.de` host. Its article body selector is
exactly `#main > .content .news-single .news-text-wrap` (quoted in the YAML
fragment). A bounded live retest on 2026-08-14 stored one clean official
article in pass 1 (`67f3b8d49d1b4bf79b9965cab0e93764`):

| Field | Evidence |
| --- | --- |
| Article | [Bunt, giftig und neu](https://www.naturzoo.de/aktuelles/artikel/bunt-giftig-und-neu) |
| Published | `2026-05-29T10:55:00+02:00` |
| Pass 1 | HTTP 200; discovered 10; fetched/parsed/stored 1; inserted 1; updated 0 |
| Pass 2 (`99415d6aabbb4206951fac24dd3ee51a`) | discovered 10; fetched/parsed/stored 0; inserted 0; updated 0; already known 1 |
| Content | 661 characters; parse success; zero quality warnings |
| Error checks | zero navigation, content, content-parse, and date errors |

The stored text contains no contact, directions, opening-hours, download, or
back-navigation contamination. The JSON and adapter plan retain the dated
run artifacts and exact source ID; no live fetch is part of artifact
validation.

## Verified rows

| Stable ID | Roster name · city · tier | Official website / host | Proposed adapter and official source URL(s) | Language · JS | `source_status` |
| --- | --- | --- | --- | --- | --- |
| `zoo-duisburg` | Zoo Duisburg · Duisburg · Priority | [zoo-duisburg.de](https://zoo-duisburg.de/) | `archive` — [news](https://zoo-duisburg.de/unser-zoo/aktuelles) | `de` · no | success |
| `zoo-krefeld` | Zoo Krefeld · Krefeld · Priority | [zookrefeld.de](https://www.zookrefeld.de/) | `archive` — [Aktuelles](https://www.zookrefeld.de/aktuelles) | `de` · no | success |
| `gruener-zoo-wuppertal` | Grüner Zoo Wuppertal · Wuppertal · Priority | [wuppertal.de/microsite/zoo](https://www.wuppertal.de/microsite/zoo/index.php) | `archive` — [Aktuelles](https://www.wuppertal.de/microsite/zoo/Aktuelles/aktuelles.php); [sitemap](https://www.wuppertal.de/microsite/zoo/sitemap.php) | `de` · yes, Cloudflare challenge | blocked |
| `zoom-erlebniswelt` | ZOOM Erlebniswelt · Gelsenkirchen · Priority | [zoom-erlebniswelt.de](https://www.zoom-erlebniswelt.de/) | `archive` — [Presse](https://www.zoom-erlebniswelt.de/presse/); [tierische Neuigkeiten](https://www.zoom-erlebniswelt.de/tierische-neuigkeiten/) | `de` · no | success |
| `zoo-dortmund` | Zoo Dortmund · Dortmund · Priority | [dortmund.de/zoo-dortmund](https://www.dortmund.de/themen/freizeit-und-kultur/parks-seen-und-zoo/zoo-dortmund/) | `archive` — [Zoo page with Nachrichten](https://www.dortmund.de/themen/freizeit-und-kultur/parks-seen-und-zoo/zoo-dortmund/); [city press index](https://www.dortmund.de/newsroom/presse-mitteilungen/) | `de` · no | success |
| `allwetterzoo-muenster` | Allwetterzoo Münster · Münster · Priority | [allwetterzoo.de](https://www.allwetterzoo.de/) | `archive` — [News](https://www.allwetterzoo.de/de/ueber-den-zoo/news/) | `de` · no | success |
| `aachener-tierpark-euregiozoo` | Aachener Tierpark Euregiozoo · Aachen · Priority | [euregiozoo.de](https://www.euregiozoo.de/) | `rss` — [RSS](https://www.euregiozoo.de/de/kontakt/rss.php) | `de` · no | success |
| `tierpark-fossilium-bochum` | Tierpark + Fossilium Bochum · Bochum · Priority | [tierpark-bochum.de](https://www.tierpark-bochum.de/) | `archive` — [Aktuelles](https://www.tierpark-bochum.de/neuigkeiten/aktuelles) | `de` · no | success |
| `aquazoo-loebbecke-museum` | Aquazoo Löbbecke Museum · Düsseldorf · Priority | [aquazoo-duesseldorf.de](https://aquazoo-duesseldorf.de/) | `archive` — [Aktuelles](https://aquazoo-duesseldorf.de/aktuelles); [city identity](https://www.duesseldorf.de/aquazoo/) | `de` · no | success |
| `naturzoo-rheine` | NaturZoo Rheine · Rheine · Priority | [naturzoo.de](https://www.naturzoo.de/) | `archive` — [Aktuelles](https://www.naturzoo.de/aktuelles/) | `de` · no | success |
| `tiergarten-kleve` | Tiergarten Kleve · Kleve · Secondary | [tiergarten-kleve.de](https://tiergarten-kleve.de/) | `rss` — [RSS](https://tiergarten-kleve.de/feed/); [news](https://tiergarten-kleve.de/aktuelles/); [WP API](https://tiergarten-kleve.de/wp-json/) | `de` · no | success |
| `tiergarten-moenchengladbach` | Tiergarten Mönchengladbach · Mönchengladbach · Secondary | [tiergarten-moenchengladbach.de](https://www.tiergarten-moenchengladbach.de/) | `rss` — [RSS](https://www.tiergarten-moenchengladbach.de/feed/); [news](https://www.tiergarten-moenchengladbach.de/neuigkeiten/); [WP API](https://www.tiergarten-moenchengladbach.de/wp-json/) | `de` · no | success |
| `terrazoo-rheinberg` | TerraZoo Rheinberg · Rheinberg · Secondary | [terrazoo.de](https://www.terrazoo.de/) | `rss` — [RSS](https://www.terrazoo.de/?format=feed&type=rss); [Atom](https://www.terrazoo.de/?format=feed&type=atom); [press](https://www.terrazoo.de/informationen-zum-terrazoo-rheinberg/%C3%BCber-uns/in-der-presse) | `de` · no | success |
| `sea-life-oberhausen` | SEA LIFE Oberhausen · Oberhausen · Secondary | [visitsealife.com/oberhausen](https://www.visitsealife.com/oberhausen/) | `archive` — [press](https://www.visitsealife.com/oberhausen/informationen/presse/); [information](https://www.visitsealife.com/oberhausen/informationen/) | `de` · no, no feed advertised | partial |
| `tiergehege-kaisergarten` | Tiergehege Kaisergarten · Oberhausen · Secondary | [tiergehege-kaisergarten.de](https://www.tiergehege-kaisergarten.de/tg/) | `rss` — [RSS](https://www.tiergehege-kaisergarten.de/tg/feed/); [news category](https://www.tiergehege-kaisergarten.de/tg/category/tiergehege/news/); [WP API](https://www.tiergehege-kaisergarten.de/tg/wp-json/) | `de` · no | success |
| `heimat-tierpark-olderdissen` | Heimat-Tierpark Olderdissen · Bielefeld · Secondary | [bielefeld.de/tierpark](https://www.bielefeld.de/tierpark) | `archive` — [municipal page and Olderdissen news](https://www.bielefeld.de/tierpark); [press index](https://www.bielefeld.de/pressedienst) | `de` · no | success |
| `safariland-stukenbrock` | Safariland Stukenbrock · Schloß Holte-Stukenbrock · Secondary | [safariland-stukenbrock.de](https://safariland-stukenbrock.de/) | `archive` — [press](https://safariland-stukenbrock.de/infos/presse.html); [home updates/events](https://safariland-stukenbrock.de/) | `de` · no | success |
| `affen-und-vogelpark-eckenhagen` | Affen- und Vogelpark Eckenhagen · Reichshof · Secondary | [affen-und-vogelpark.de](https://affen-und-vogelpark.de/) | `rss` — [RSS](https://affen-und-vogelpark.de/feed/) | `de` · no | success |
| `wildpark-grafenberg` | Wildpark Grafenberg · Düsseldorf · Secondary | [duesseldorf.de Wildpark](https://www.duesseldorf.de/stadtgruen/wald/wildpark) | `registry_only` — [municipal identity/visit page](https://www.duesseldorf.de/stadtgruen/wald/wildpark); [broad city news index](https://www.duesseldorf.de/aktuelles/news) (not proposed for crawling) | `de` · no | partial |
| `biotopwildpark-anholter-schweiz` | Biotopwildpark Anholter Schweiz · Isselburg · Secondary | [canonical wildpark-nrw.de](https://www.wildpark-nrw.de/) (legacy [anholter-schweiz.de](https://www.anholter-schweiz.de/)) | `rss` — [canonical RSS](https://www.wildpark-nrw.de/feed/); [canonical home](https://www.wildpark-nrw.de/); [legacy visitor page](https://www.anholter-schweiz.de/besucherinformationen/) | `de` · no | partial |
| `naturwildpark-granat` | Naturwildpark Granat · Haltern am See · Secondary | [naturwildpark.de](https://www.naturwildpark.de/) | `rss` — [RSS](https://www.naturwildpark.de/feed/); [press](https://www.naturwildpark.de/presse/) | `de` · no | success |
| `wildwald-vosswinkel` | Wildwald Vosswinkel · Arnsberg · Secondary | [wildwald.de](https://wildwald.de/) | `archive` — [Aktuelles](https://wildwald.de/aktuelles/) | `de` · no | success |
| `hochwildpark-rheinland` | Hochwildpark Rheinland · Mechernich · Secondary | [hochwildpark-rheinland.de](https://www.hochwildpark-rheinland.de/) | `rss` — [RSS](https://www.hochwildpark-rheinland.de/feed/); [home/news](https://www.hochwildpark-rheinland.de/); [city identity](https://www.mechernich.de/tourismus-und-freizeit/erlebenswert/hochwildpark-kommern) | `de` · no | success |
| `zoo-im-brueckenkopfpark` | Zoo im Brückenkopfpark · Jülich · Secondary | [brueckenkopf-park.de](https://brueckenkopf-park.de/) | `archive` — [events](https://brueckenkopf-park.de/events/); [zoo identity](https://brueckenkopf-park.de/tiere/) | `de` · no, no feed advertised | partial |
| `adlerwarte-berlebeck` | Adlerwarte Berlebeck · Detmold · Secondary | [detmold-adlerwarte.de](https://www.detmold-adlerwarte.de/) | `archive` — [Aktuelles](https://www.detmold-adlerwarte.de/aktuelles/); [sitemap](https://www.detmold-adlerwarte.de/sitemap.xml); [municipal identity](https://www.detmold.de/verwaltung/adlerwarte) | `de` · no | success |

## Gap and access notes

- **Grüner Zoo Wuppertal:** the official city microsite and its `Aktuelles`
  archive are indexed and show current first-party entries, but direct HTTP
  access during this check returned a Cloudflare challenge. The proposed
  `archive` adapter should remain blocked until a robots-compliant fetch path
  is confirmed; no browser fallback is assumed.
- **SEA LIFE Oberhausen:** the official Oberhausen site and press page are
  clear, but no dedicated RSS/Atom/API/news archive was advertised. It is
  retained as `partial` with a narrow press-only `archive` proposal.
- **Wildpark Grafenberg:** the Düsseldorf municipal identity page is clear,
  but no Wildpark-specific news feed was found. The broad city news index is
  evidence only and is explicitly not proposed as a crawl source; use
  `registry_only` unless a narrow city-tagged endpoint is approved.
- **Biotopwildpark Anholter Schweiz:** the familiar official domain currently
  uses the branded `wildpark-nrw.de` canonical host. Both hosts are retained
  as evidence; the canonical feed is proposed and the row is `partial` until
  the migration is confirmed stable.
- **Zoo im Brückenkopfpark:** the official park site clearly identifies the
  zoo and exposes event detail pages, but no dedicated zoo-news feed was
  found. It is retained as `partial` with an events-only `archive` proposal.

All remaining rows have a first-party news, press, RSS, Atom, or API surface
on the official/operator or municipal domain. No URL outside the official
site/operator/municipal domains was selected as a crawl source.
