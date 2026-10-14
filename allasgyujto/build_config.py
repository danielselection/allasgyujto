#!/usr/bin/env python3
"""A `companies` gyűjtemény (JSON-export) alapján elkészíti a collector companies.json-ját:
az URL-ekből felismeri az ATS-t, és kiválasztja a legjobb (ingyenes, API-alapú) módszert.

Használat: python3 build_config.py <companies-export-mappa> > companies.json
"""
import glob
import json
import re
import sys
from urllib.parse import urlparse

RENDER_HOSTS = ("karrierportal.hu", "DataCenter/Registration", "notino.com", "telekom.hu",
                "jobylon.com", "talentlyft.com", "barion.com", "henkel.com", "lidl.hu",
                "erstebank.hu", "kh.hu", "bmwgroup.jobs")


def detect(d: dict) -> dict | None:
    urls = [d.get(k) or "" for k in ("feed_url", "search_url", "list_url", "career_url")]
    allu = " ".join(urls)
    first = next((u for u in urls if u), "")
    if not first:
        return None
    if m := re.search(r"https://([\w-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", allu):
        return {"ats": "workday", "url": f"https://{m.group(1)}/{m.group(2)}"}
    if m := re.search(r"smartrecruiters\.com/v1/companies/([\w-]+)", allu):
        return {"ats": "smartrecruiters", "id": m.group(1)}
    if m := re.search(r"greenhouse\.io/([\w-]+)", allu):
        return {"ats": "greenhouse", "id": m.group(1)}
    if m := (re.search(r"workable\.com/api/v\d/widget/accounts/([\w-]+)", allu)
             or re.search(r"apply\.workable\.com/(?!api/)([\w-]+)", allu)):
        return {"ats": "workable", "id": m.group(1)}
    if m := re.search(r"([\w-]+)\.jobs\.personio\.(?:com|de)", allu):
        return {"ats": "personio", "id": m.group(1)}
    if m := re.search(r"([\w-]+)\.bamboohr\.com", allu):
        return {"ats": "bamboohr", "id": m.group(1)}
    if m := re.search(r"([\w-]+)\.recruitee\.com", allu):
        return {"ats": "recruitee", "id": m.group(1)}
    if d.get("feed_type") == "rss" and d.get("feed_url"):
        return {"ats": "rss", "url": d["feed_url"]}
    if d.get("feed_type") == "sitemap" and d.get("feed_url"):
        return {"ats": "sitemap", "url": d["feed_url"]}
    if any(h in allu for h in RENDER_HOSTS):
        return {"ats": "render", "url": d.get("list_url") or d.get("search_url") or first,
                "waitMs": 6000, "filterHU": False}
    su = d.get("search_url") or ""
    if re.search(r"/(search|go/[^?]+)/?\?(.*&)?q=\{kw\}", su) or "jobs.hr.cloud.sap" in su:
        return {"ats": "successfactors", "url": su}
    if "search-results" in allu and "{kw}" not in (d.get("list_url") or "x{kw}"):
        return {"ats": "phenom", "url": d.get("list_url") or su}
    if re.search(r"search-results\?keywords=\{kw\}|careers\?query=\{kw\}", su):
        return {"ats": "phenom", "url": su}
    if "taleo.net" in allu:
        return {"ats": "rss", "url": d.get("feed_url")} if d.get("feed_url") else None
    return {"ats": "html", "url": su or d.get("list_url") or first,
            "filterHU": bool(re.search(r"careers\.|jobs\.", su or first))}


def main(folder: str):
    out = []
    for fn in sorted(glob.glob(f"{folder}/*.json")):
        raw = json.load(open(fn))
        d = raw.get("data", raw)
        slug = raw.get("id") or fn.rsplit("/", 1)[-1][:-5]
        if d.get("active") is False or d.get("index_method") == "none":
            continue
        cfg = detect(d)
        if not cfg:
            continue
        out.append({"slug": slug, "name": d.get("name", slug), **cfg,
                    "liked": bool(d.get("liked")), "target": bool(d.get("target"))})
    print(json.dumps({"companies": out}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
