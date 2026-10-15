#!/usr/bin/env python3
"""
Állásgyűjtő – determinisztikus, skálázható karrieroldal-gyűjtő a Vezetői Állásfigyelőhöz.

A cégeket az ATS-ük (állásportál-szoftverük) szerint kezeli: ahol van nyilvános JSON/XML
végpont, azt kérdezi le (ingyenes, stabil), ahol nincs, szerveroldali HTML-t elemez, és csak
végső esetben indít fej nélküli böngészőt (Playwright/Chromium). Firecrawl-t nem használ.

Kimenet (out/):
  jobs.json      – minden magyarországi nyitott állás cégenként, egységes formában
  diff.json      – az előző futáshoz képest új és eltűnt állások (lezárás-jelölt)
  report.json    – cégenkénti állapot (ok / empty / fail, darabszám, módszer, hiba)

Használat:
  python3 collector.py --config companies.json [--only essity,bosch] [--no-render]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36 Allasgyujto/0.1")
TIMEOUT = 25
HU_RE = re.compile(r"budapest|hungary|magyarorsz|\bHU\b|\bdebrecen|\bszeged|\bgyőr\b|\bgyor\b|\bpécs\b|\bpecs\b|"
                   r"remote.{0,12}\b(hungary|hu)\b|törökbálint|budaörs|\bbicske|\bvecsés", re.I)
# Vezetői + terület előszűrő (csak jelölés, nem dob el semmit: a döntés az ágenseké)
LEAD_RE = re.compile(r"head|director|igazgat|vezet|lead|manager|chief|cmo|cdo|vp\b|owner|"
                     r"chapter|csoportvezet|osztályvezet", re.I)
HU_URL_RE = re.compile(r"budapest|hungary|magyarorszag", re.I)  # URL-ben csak ezek számítanak (PECS ≠ Pécs)
MULTI_LOC_RE = re.compile(r"\d+\s+(Locations?|telephely|Standorte?|helysz[ií]n|lokalit)", re.I)
AREA_RE = re.compile(r"marketing|brand|márka|kommunik|communicat|digital|digitál|growth|"
                     r"performance|e-?commerce|omnichannel|crm|loyalty|lifecycle|martech|"
                     r"customer|ügyfél|channel|csatorn|product marketing|ai\b|transform|"
                     r"excellence|retention|cvm|engagement|media|média|content|social", re.I)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Fetcher:
    def __init__(self, delay: float = 0.6):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8"})
        self.delay = delay
        self.calls = 0

    def _req(self, method, url, **kw):
        for attempt in range(3):
            self.calls += 1
            r = self.s.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code in (429, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            time.sleep(self.delay)
            r.raise_for_status()
            return r
        r.raise_for_status()

    def get(self, url, **kw):
        return self._req("GET", url, **kw)

    def post_json(self, url, payload, **kw):
        return self._req("POST", url, json=payload,
                         headers={"Accept": "application/json", "Content-Type": "application/json"}, **kw)


TITLE_CUT = re.compile(r"\s+(Location:|Category:|Helysz[ií]n:|Kateg[oó]ria:|Posted|Közzétéve).*$|\s+Budapest\s*,.*$", re.I)
TITLE_PREFIX = re.compile(r"^(Save job|Save for Later|Mentés|Oszd meg[^:]*:)\s*", re.I)


def clean_title(t):
    t = html.unescape(re.sub(r"\s+", " ", t or "")).strip()
    t = TITLE_PREFIX.sub("", t)
    t = TITLE_CUT.sub("", t).strip(" ,|-")
    return t


def job(company, title, url, location="", ad_id="", posted="", extra=None):
    title = clean_title(title)
    location = html.unescape(re.sub(r"\s+", " ", location or "")).strip()
    j = {"company": company, "title": title, "url": url, "location": location,
         "adId": str(ad_id or ""), "postedAt": posted or "",
         "match": bool(AREA_RE.search(title)) and bool(LEAD_RE.search(title))}
    if extra:
        j.update(extra)
    return j


# ----------------------------------------------------------------------------- adapterek

def ad_workday(f: Fetcher, c: dict, kws: list[str]) -> list[dict]:
    """Workday CXS JSON API: POST /wday/cxs/{tenant}/{site}/jobs."""
    u = urlparse(c["url"])
    tenant = u.netloc.split(".")[0]
    parts = [p for p in u.path.split("/") if p]
    site = parts[-1] if parts and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[-1]) else parts[0]
    api = f"https://{u.netloc}/wday/cxs/{tenant}/{site}/jobs"
    out, seen = [], set()
    for kw in list(kws) + ["Hungary", "Budapest"]:
        for offset in range(0, 200, 20):
            data = f.post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                                     "searchText": kw}).json()
            posts = data.get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                loc = p.get("locationsText", "")
                if MULTI_LOC_RE.search(loc):
                    try:  # több helyszínes: részletek
                        d = f.get(f"https://{u.netloc}/wday/cxs/{tenant}/{site}{path}",
                                  headers={"Accept": "application/json"}).json()
                        info = d.get("jobPostingInfo", {})
                        loc = "; ".join([info.get("location", "")] + info.get("additionalLocations", []))
                    except Exception:
                        pass
                if (HU_RE.search(loc) and not MULTI_LOC_RE.search(loc)) or HU_URL_RE.search(path):
                    rid = (p.get("bulletFields") or [""])[0]
                    out.append(job(c["name"], p.get("title"), f"https://{u.netloc}/{site}{path}",
                                   loc, rid, p.get("postedOn", "")))
            if len(posts) < 20:
                break
    return out


def ad_smartrecruiters(f, c, kws):
    cid = c["id"]
    out, seen = [], set()
    for kw in kws or [""]:
        for offset in range(0, 500, 100):
            d = f.get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings",
                      params={"q": kw, "country": "hu", "limit": 100, "offset": offset}).json()
            for p in d.get("content", []):
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                loc = p.get("location", {})
                out.append(job(c["name"], p.get("name"), f"https://jobs.smartrecruiters.com/{cid}/{p['id']}",
                               f"{loc.get('city', '')}, {loc.get('country', '')}", p.get("refNumber") or p["id"],
                               p.get("releasedDate", "")))
            if offset + 100 >= d.get("totalFound", 0):
                break
    return out


def ad_greenhouse(f, c, kws):
    d = f.get(f"https://boards-api.greenhouse.io/v1/boards/{c['id']}/jobs").json()
    return [job(c["name"], p["title"], p["absolute_url"], p.get("location", {}).get("name", ""),
                p["id"], p.get("updated_at", ""))
            for p in d.get("jobs", []) if HU_RE.search(p.get("location", {}).get("name", ""))]


def ad_lever(f, c, kws):
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    d = f.get(f"https://{host}/v0/postings/{c['id']}?mode=json").json()
    return [job(c["name"], p["text"], p["hostedUrl"], p.get("categories", {}).get("location", ""),
                p["id"], dt.datetime.fromtimestamp(p.get("createdAt", 0) / 1000, dt.timezone.utc).date().isoformat())
            for p in d if HU_RE.search(json.dumps(p.get("categories", {}), ensure_ascii=False))]


def ad_workable(f, c, kws):
    d = f.get(f"https://apply.workable.com/api/v1/widget/accounts/{c['id']}").json()
    out = []
    for p in d.get("jobs", []):
        loc = ", ".join(filter(None, [p.get("city"), p.get("country")]))
        locs = json.dumps(p.get("locations", []), ensure_ascii=False)
        if HU_RE.search(loc + locs) or p.get("telecommuting"):
            out.append(job(c["name"], p["title"], p.get("url") or p.get("application_url"), loc or locs,
                           p.get("shortcode"), p.get("published_on", "")))
    return out


def ad_recruitee(f, c, kws):
    d = f.get(f"https://{c['id']}.recruitee.com/api/offers/").json()
    return [job(c["name"], p["title"], p["careers_url"], f"{p.get('city', '')}, {p.get('country', '')}",
                p["id"], p.get("published_at", ""))
            for p in d.get("offers", []) if HU_RE.search(f"{p.get('city')} {p.get('country')} {p.get('location')}")]


def ad_personio(f, c, kws):
    last = None
    for host in (f"{c['id']}.jobs.personio.com", f"{c['id']}.jobs.personio.de"):
        try:
            root = ET.fromstring(f.get(f"https://{host}/xml?language=en").content)
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        raise last
    out = []
    for p in root.iter("position"):
        g = lambda t: (p.findtext(t) or "")
        out.append(job(c["name"], g("name"), f"https://{host}/job/{g('id')}",
                       g("office"), g("id"), g("createdAt")))
    return out


def ad_bamboohr(f, c, kws):
    d = f.get(f"https://{c['id']}.bamboohr.com/careers/list", headers={"Accept": "application/json"}).json()
    out = []
    for p in d.get("result", []):
        loc = p.get("location") or {}
        locs = ", ".join(filter(None, [loc.get("city"), loc.get("state"), p.get("atsLocation", {}).get("country")]))
        if HU_RE.search(locs + json.dumps(p.get("atsLocation", {}))) or p.get("isRemote"):
            out.append(job(c["name"], p["jobOpeningName"], f"https://{c['id']}.bamboohr.com/careers/{p['id']}",
                           locs, p["id"]))
    return out


def ad_rss(f, c, kws):
    root = ET.fromstring(f.get(c["url"]).content)
    out = []
    for it in root.iter("item"):
        t, link = it.findtext("title") or "", it.findtext("link") or ""
        desc = it.findtext("description") or ""
        if c.get("filterHU", True) and not HU_RE.search(t + desc + link):
            continue
        out.append(job(c["name"], t, link, "", re.sub(r"\D", "", link)[-10:], it.findtext("pubDate") or ""))
    return out


def ad_sitemap(f, c, kws):
    root = ET.fromstring(f.get(c["url"]).content)
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    out = []
    for loc in root.iter(f"{ns}loc"):
        u = loc.text or ""
        if not HU_URL_RE.search(u):
            continue
        slug = urlparse(u).path.rstrip("/").split("/")[-1]
        title = re.sub(r"[-_]+", " ", re.sub(r"_?R?\d{5,}.*$", "", slug)).strip()
        out.append(job(c["name"], title, u, "", re.findall(r"\d{5,}", u)[-1] if re.findall(r"\d{5,}", u) else ""))
    return out


def ad_successfactors(f, c, kws):
    """SAP SuccessFactors Career Site Builder (/search/?q=) és SAP RMK (jobs.hr.cloud.sap)."""
    out, seen = [], set()
    for kw in kws:
        for start in range(0, 250, 25):
            url = c["url"].replace("{kw}", quote(kw))
            url += ("&" if "?" in url else "?") + f"startrow={start}"
            soup = BeautifulSoup(f.get(url).text, "lxml")
            rows = soup.select("tr.data-row") or soup.select("li.job-tile, div.job-tile")
            for r in rows:
                a = r.select_one("a.jobTitle-link") or r.select_one("a[href*='/job/']")
                if not a:
                    continue
                href = urljoin(url, a["href"])
                if href in seen:
                    continue
                seen.add(href)
                loc = (r.select_one(".jobLocation") or r.select_one("[data-col='location']") or a)
                ltxt = loc.get_text(" ")
                cc = re.search(r",\s*([A-Z]{2})\b", ltxt)
                if cc and cc.group(1) != "HU" and not HU_RE.search(ltxt):
                    continue
                out.append(job(c["name"], a.get_text(), href, ltxt,
                               (re.findall(r"/(\d{6,})/?", href) or [""])[-1],
                               (r.select_one(".jobDate") or a).get_text(" ") if r.select_one(".jobDate") else ""))
            if len(rows) < 25:
                break
    return out


def ad_phenom(f, c, kws):
    """Phenom: az oldal HTML-jében lévő phApp.ddo JSON (eagerLoadRefineSearch)."""
    out, seen = [], set()
    for kw in kws or [""]:
      url0 = c["url"].replace("{kw}", quote(kw))
      for start in range(0, 300, 10):  # Phenom: 10 találat / oldal, &from=N&s=1
        url = url0 + (f"&from={start}&s=1" if start else "")
        txt = f.get(url).text
        m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.", txt, re.S)
        if not m:
            raise RuntimeError("phApp.ddo nem található (render kell?)")
        ers = json.loads(m.group(1)).get("eagerLoadRefineSearch", {})
        jobs = ers.get("data", {}).get("jobs", [])
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}{c.get('jobPathPrefix', '/global/en/job/')}"
        if start + len(jobs) >= ers.get("totalHits", 0) or not jobs:
            last = True
        else:
            last = False
        for p in jobs:
            jid = p.get("jobId") or p.get("reqId")
            if jid in seen:
                continue
            seen.add(jid)
            loc = p.get("location") or p.get("cityStateCountry") or ""
            if HU_RE.search(json.dumps(p.get("multi_location", []), ensure_ascii=False) + loc):
                out.append(job(c["name"], p.get("title"), base + str(jid), loc, jid, p.get("postedDate", "")))
        if last:
            break
    return out


NAV_RE = re.compile(r"^(skip to|previous|next|show all|back to top|jobs you|english|français|deutsch|magyar|"
                    r"search|keres[eé]s|összes|all jobs|view all|load more|tov[aá]bb|vissza|bejelentkez|login|"
                    r"sign in|apply now|jelentkez[eé]s|details|r[eé]szletek)\b", re.I)
FOREIGN_RE = re.compile(r"\b(Korea|India|Poland|Polska|Romania|Rom[aâ]nia|Czech|Slovakia|Germany|Deutschland|"
                        r"France|Sweden|Spain|Italy|Portugal|Mexico|Brazil|China|Malaysia|Philippines|"
                        r"United States|USA|United Kingdom|Ireland|Austria|Croatia|Serbia|Bulgaria)\b")


def ad_html_links(f, c, kws, html_text=None):
    """Általános: a megadott lista/keresőoldal linkjei, amelyek mintája (linkPattern) illeszkedik."""
    out, seen = [], set()
    urls = [c["url"].replace("{kw}", quote(k)) for k in kws] if "{kw}" in c["url"] else [c["url"]]
    pat = re.compile(c.get("linkPattern", r"/(job|jobs|allas|allasok|position|JobDetail|vacanc)[^?#]*\d|/allasok/[^/?#]+$"), re.I)
    for url in urls:
        text = html_text if html_text is not None else f.get(url).text
        soup = BeautifulSoup(text, "lxml")
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            t = a.get_text(" ", strip=True)
            if not pat.search(href) or href in seen or len(t) < 4 or NAV_RE.search(t):
                continue
            if FOREIGN_RE.search(t) and not HU_RE.search(t):
                continue
            seen.add(href)
            ctx = a.find_parent(["li", "tr", "article", "div"])
            loc = ctx.get_text(" ", strip=True)[:300] if ctx else ""
            if c.get("filterHU") and not HU_RE.search(loc + href):
                continue
            out.append(job(c["name"], t, href, loc[:120], (re.findall(r"(\d{5,})", href) or [""])[-1]))
    return out


_PW = None


def render(url: str, wait_ms: int = 6000) -> str:
    """Fej nélküli Chromium (Playwright). Csak a JS-es oldalakhoz. Szálanként saját példány."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            pg = b.new_page(user_agent=UA, locale="hu-HU")
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(wait_ms)
            return pg.content()
        finally:
            b.close()


def ad_render(f, c, kws):
    urls = [c["url"].replace("{kw}", quote(k)) for k in kws] if "{kw}" in c["url"] else [c["url"]]
    out = []
    for u in urls:
        out += ad_html_links(f, {**c, "url": u}, [], html_text=render(u, c.get("waitMs", 6000)))
    return out


ADAPTERS = {
    "workday": ad_workday, "smartrecruiters": ad_smartrecruiters, "greenhouse": ad_greenhouse,
    "lever": ad_lever, "workable": ad_workable, "recruitee": ad_recruitee, "personio": ad_personio,
    "bamboohr": ad_bamboohr, "rss": ad_rss, "sitemap": ad_sitemap, "successfactors": ad_successfactors,
    "phenom": ad_phenom, "html": ad_html_links, "render": ad_render,
}
# ha a fő módszer hibázik vagy üres vázat ad, ezzel próbálja újra
FALLBACK = {"phenom": "render", "html": "render", "successfactors": "render"}


# ----------------------------------------------------------------------------- futtatás

def run_company(c: dict, kws: list[str], allow_render: bool) -> tuple[list[dict], dict]:
    f = Fetcher()
    methods = [c["ats"]] + ([FALLBACK[c["ats"]]] if c["ats"] in FALLBACK else [])
    rep = {"company": c["name"], "slug": c["slug"], "ats": c["ats"], "status": "fail", "count": 0, "error": ""}
    for m in methods:
        if m == "render" and not allow_render:
            continue
        t0 = time.time()
        try:
            jobs = ADAPTERS[m](f, c, kws)
            # dedup URL szerint
            uniq = list({j["url"]: {**j, "slug": c["slug"], "method": m} for j in jobs}.values())
            rep.update(status="ok" if uniq else "empty", count=len(uniq), method=m,
                       requests=f.calls, secs=round(time.time() - t0, 1), error="")
            if uniq or m == methods[-1]:
                return uniq, rep
        except Exception as e:  # noqa: BLE001
            rep.update(error=f"{m}: {type(e).__name__}: {str(e)[:160]}", method=m)
    return [], rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="companies.json")
    ap.add_argument("--keywords", default="keywords.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", default="")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text())
    kwcfg = json.loads(Path(a.keywords).read_text()) if Path(a.keywords).exists() else {}
    base_kws = kwcfg.get("base", ["marketing", "digital", "CRM", "brand", "head", "director"])
    only = set(filter(None, a.only.split(",")))
    companies = [c for c in cfg["companies"] if c.get("ats") and (not only or c["slug"] in only)]

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    prev = {}
    if (outdir / "jobs.json").exists():
        for j in json.loads((outdir / "jobs.json").read_text())["jobs"]:
            prev[j["url"]] = j

    all_jobs, reports = [], []
    # a böngészős (render) cégek sorban futnak, a többi párhuzamosan
    light = [c for c in companies if c["ats"] != "render"]
    heavy = [c for c in companies if c["ats"] == "render"]
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(run_company, c, c.get("keywords", base_kws), False): c for c in light}
        for fu in cf.as_completed(futs):
            j, r = fu.result()
            all_jobs += j
            reports.append(r)
    second = heavy + [c for c in light if any(r["slug"] == c["slug"] and r["status"] != "ok" for r in reports)
                      and c["ats"] in FALLBACK and not a.no_render]
    with cf.ThreadPoolExecutor(4) as ex:  # böngészős kör: 4 párhuzamos Chromium
        futs = {ex.submit(run_company, c, c.get("keywords", base_kws), not a.no_render): c for c in second}
        for fu in cf.as_completed(futs):
            c = futs[fu]
            j, r = fu.result()
            reports = [x for x in reports if x["slug"] != c["slug"]] + [r]
            all_jobs = [x for x in all_jobs if x["slug"] != c["slug"]] + j

    ts = now_iso()
    for j in all_jobs:
        j["firstSeen"] = prev.get(j["url"], {}).get("firstSeen", ts)
        j["lastSeen"] = ts
    ok_slugs = {r["slug"] for r in reports if r["status"] == "ok"}
    cur = {j["url"] for j in all_jobs}
    diff = {
        "generatedAt": ts,
        "new": [j for j in all_jobs if j["url"] not in prev],
        # csak olyan cégnél jelöljük eltűntnek, ahol a mai lekérés sikeres és nem üres volt
        "disappeared": [p for u, p in prev.items() if u not in cur and p["slug"] in ok_slugs],
    }
    (outdir / "jobs.json").write_text(json.dumps({"generatedAt": ts, "count": len(all_jobs), "jobs": all_jobs},
                                                 ensure_ascii=False, indent=1))
    (outdir / "diff.json").write_text(json.dumps(diff, ensure_ascii=False, indent=1))
    summary = {s: sum(r["status"] == s for r in reports) for s in ("ok", "empty", "fail")}
    (outdir / "report.json").write_text(json.dumps({"generatedAt": ts, "summary": summary,
                                                    "companies": sorted(reports, key=lambda r: r["slug"])},
                                                   ensure_ascii=False, indent=1))
    print(json.dumps({"summary": summary, "jobs": len(all_jobs), "new": len(diff["new"]),
                      "disappeared": len(diff["disappeared"]),
                      "matches": sum(j["match"] for j in all_jobs)}, ensure_ascii=False))
    if _PW:
        _PW.stop()


if __name__ == "__main__":
    sys.exit(main())
