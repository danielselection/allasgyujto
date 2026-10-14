"""Hálózat nélküli tesztek: minta-válaszokkal ellenőrzi az adaptereket, a böngészős
renderelést (helyi fájlon) és az új/eltűnt állások különbségképzését."""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import collector as C


class Resp:
    def __init__(self, data=None, text="", content=b""):
        self._d, self.text, self.content, self.status_code = data, text, content or text.encode(), 200

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


WD = {"jobPostings": [
    {"title": "Head of Digital Marketing", "externalPath": "/job/Budapest/Head-of-Digital-Marketing_R123456",
     "locationsText": "Budapest, Hungary", "postedOn": "Posted 3 Days Ago", "bulletFields": ["R123456"]},
    {"title": "Brand Manager", "externalPath": "/job/Stockholm/Brand-Manager_R1", "locationsText": "Stockholm"},
    {"title": "CRM Lead", "externalPath": "/job/x/CRM-Lead_R2", "locationsText": "3 Locations"}]}
WD_DETAIL = {"jobPostingInfo": {"location": "Gothenburg", "additionalLocations": ["Budapest, Hungary"]}}

SR = {"totalFound": 1, "content": [{"id": "744000", "name": "Marketing Team Lead", "refNumber": "REF1",
                                    "location": {"city": "Budapest", "country": "hu"}, "releasedDate": "2026-10-10"}]}

SF_HTML = """<table><tr class="data-row"><td><a class="jobTitle-link" href="/job/Budapest-Marketingvezet%C5%91/1234567/">
Marketingvezető</a></td><td><span class="jobLocation">Budapest, HU</span></td><td><span class="jobDate">2026. okt. 10.</span></td></tr>
<tr class="data-row"><td><a class="jobTitle-link" href="/job/Budapest-Accountant/7654321/">Accountant</a></td>
<td><span class="jobLocation">Budapest, HU</span></td></tr></table>"""

PH_HTML = 'x<script>phApp.ddo = {"eagerLoadRefineSearch":{"data":{"jobs":[{"title":"Omnichannel Lead","jobId":"202610","location":"Budapest, Hungary","postedDate":"2026-10-01"},{"title":"Sales Rep","jobId":"9","location":"Basel, Switzerland"}]}}}; phApp.sessionParams = {};</script>'

PERSONIO = b"""<workzag-jobs><position><id>99</id><office>Budapest</office><name>Head of Growth</name>
<createdAt>2026-10-09T10:00:00+00:00</createdAt></position></workzag-jobs>"""


def fake_get(self, url, **kw):
    self.calls += 1
    if "wday/cxs" in url:
        return Resp(WD_DETAIL)
    if "smartrecruiters" in url:
        return Resp(SR)
    if "phenom.test" in url:
        return Resp(text=PH_HTML)
    if "personio" in url:
        return Resp(content=PERSONIO)
    if "sf.test" in url:
        return Resp(text=SF_HTML if "startrow=0" in url else "")
    raise AssertionError(url)


def fake_post(self, url, payload, **kw):
    self.calls += 1
    return Resp(WD if payload["offset"] == 0 else {"jobPostings": []})


def check(name, cond):
    print(("OK  " if cond else "HIBA") + " " + name)
    return cond


def main():
    ok = True
    with mock.patch.object(C.Fetcher, "get", fake_get), mock.patch.object(C.Fetcher, "post_json", fake_post):
        f = C.Fetcher(delay=0)
        wd = C.ad_workday(f, {"name": "Essity", "url": "https://essity.wd3.myworkdayjobs.com/Job_opportunities"}, ["marketing"])
        ok &= check("workday: budapesti + több helyszínes (részletekből) megvan, stockholmi kiszűrve",
                    [j["title"] for j in wd] == ["Head of Digital Marketing", "CRM Lead"] and wd[0]["adId"] == "R123456")
        sr = C.ad_smartrecruiters(f, {"name": "Bosch", "id": "BoschGroup"}, ["marketing"])
        ok &= check("smartrecruiters", sr[0]["url"].endswith("/BoschGroup/744000") and sr[0]["match"])
        sf = C.ad_successfactors(f, {"name": "OTP", "url": "https://sf.test/search/?q={kw}"}, ["marketing"])
        ok &= check("successfactors: 2 sor, a marketingvezető match, a könyvelő nem",
                    len(sf) == 2 and sf[0]["match"] and not sf[1]["match"] and sf[0]["adId"] == "1234567")
        ph = C.ad_phenom(f, {"name": "Roche", "url": "https://phenom.test/search-results?keywords={kw}"}, ["x"])
        ok &= check("phenom: csak a budapesti", len(ph) == 1 and ph[0]["title"] == "Omnichannel Lead")
        pe = C.ad_personio(f, {"name": "Szallas", "id": "szallasgroup"}, [])
        ok &= check("personio XML", pe[0]["title"] == "Head of Growth" and pe[0]["match"])

    # böngészős renderelés helyi, JS-sel töltött oldalon
    tmp = Path(tempfile.mkdtemp())
    page = tmp / "nexum.html"
    page.write_text("""<html><body><div id=l></div><script>
      setTimeout(()=>{document.getElementById('l').innerHTML=
      '<ul><li><a href="/allasok/marketing-igazgato-123456">Marketingigazgató</a> Budapest</li></ul>'},300);
    </script></body></html>""")
    try:
        rj = C.ad_render(C.Fetcher(), {"name": "MVM", "url": page.as_uri(), "waitMs": 1200}, [])
        ok &= check("render (Playwright, JS-sel betöltött lista)", len(rj) == 1 and rj[0]["title"] == "Marketingigazgató")
    except Exception as e:  # noqa: BLE001
        ok &= check(f"render: {e}", False)

    # különbségképzés: két futás, a második napra az egyik állás eltűnik
    cfg = tmp / "companies.json"
    cfg.write_text(json.dumps({"companies": [{"slug": "szallas", "name": "Szallas", "ats": "personio", "id": "szallasgroup"}]}))
    out = tmp / "out"
    global PERSONIO
    with mock.patch.object(C.Fetcher, "get", fake_get):
        sys.argv = ["x", "--config", str(cfg), "--out", str(out), "--no-render"]
        C.main()
        d1 = json.loads((out / "diff.json").read_text())
        PERSONIO = PERSONIO.replace(b"<id>99</id>", b"<id>100</id>").replace(b"Head of Growth", b"Head of CRM")
        C.main()
        d2 = json.loads((out / "diff.json").read_text())
    ok &= check("diff: 1. nap 1 új; 2. nap 1 új + 1 eltűnt",
                len(d1["new"]) == 1 and len(d2["new"]) == 1 and len(d2["disappeared"]) == 1
                and d2["disappeared"][0]["title"] == "Head of Growth")
    print("\nMINDEN TESZT RENDBEN" if ok else "\nVAN HIBÁS TESZT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
