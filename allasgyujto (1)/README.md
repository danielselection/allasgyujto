# Állásgyűjtő (prototípus)

Determinisztikus karrieroldal-gyűjtő a Vezetői Állásfigyelőhöz. A cégeket az állásportál-szoftverük
(ATS) szerint kezeli: ahol van nyilvános JSON/XML-végpont, azt kérdezi le, ahol nincs, a szerveroldali
HTML-t elemzi, és csak végső esetben indít fej nélküli böngészőt. Firecrawl-kreditet nem használ.

| Módszer | Cégek (most) | Hogyan |
|---|---|---|
| workday | Essity, Mondelez, Ayvens, GoTo, Shell | `POST /wday/cxs/{tenant}/{site}/jobs` JSON |
| smartrecruiters | Bosch, MET | nyilvános `api.smartrecruiters.com` |
| greenhouse / workable / personio / bamboohr | Wolt, Egon Zehnder, Szallas, Prezi | nyilvános API / XML |
| phenom | Roche, Bayer, P&G, PMI, ABB | az oldalba ágyazott `phApp.ddo` JSON, lapozással |
| successfactors | OTP, Wizz, Nestlé, Yettel, Richter… (19) | szerveroldali HTML-tábla |
| rss / sitemap | MOL (Taleo), Wise | feed |
| render | Nexum (MVM, Raiffeisen, Uniqa…), HRmaster, Telekom, Notino… (19) | Playwright + Chromium |
| html | a többi (39) | linkkinyerés mintával |

Kimenet: `out/jobs.json` (minden magyarországi nyitott állás), `out/diff.json` (új és eltűnt
állások), `out/report.json` (cégenkénti állapot).

Fájlok: `collector.py` (gyűjtő), `build_config.py` (a `companies` gyűjteményből ATS-felismerés),
`sync_config.py` (napi, biztonságos frissítés: csak az új aktív cégeket veszi fel, a kézzel hangolt bejegyzéseket nem bántja; az esti ágensfutás hívja), `companies.json` (konfiguráció), `test_offline.py` (hálózat nélküli tesztek),
`.github/workflows/collect.yml` (napi futtatás GitHub Actionsben; az eredmény az `out/` mappába kerül, az ágensek a raw.githubusercontent.com címről olvassák).
