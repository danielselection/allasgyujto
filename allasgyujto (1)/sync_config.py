#!/usr/bin/env python3
"""A companies.json biztonságos frissítése a `companies` gyűjtemény exportjából.

A build_config.py-val ellentétben NEM generál újra mindent: a meglévő (kézzel finomhangolt)
bejegyzésekhez nem nyúl, csak
  - felveszi az új, aktív cégeket (pl. amelyeket a Cégfelderítő ágens hozott), ATS-felismeréssel,
  - frissíti a liked/target jelzőt,
  - kiveszi azokat, amelyek az adatbázisban active = false vagy index_method = "none" lettek.

Használat: python3 sync_config.py <companies-export-mappa> [companies.json]
A végén egy sorban kiírja a változásokat (stderr); ha nincs változás, a fájl nem módosul.
"""
import datetime as dt
import glob
import json
import sys

from build_config import detect


def main(folder: str, cfg_path: str = "companies.json"):
    cfg = json.load(open(cfg_path))
    cur = {c["slug"]: c for c in cfg["companies"]}
    added, removed, flagged = [], [], 0
    seen = set()
    for fn in sorted(glob.glob(f"{folder}/*.json")):
        raw = json.load(open(fn))
        d = raw.get("data", raw)
        slug = raw.get("id") or fn.rsplit("/", 1)[-1][:-5]
        seen.add(slug)
        inactive = d.get("active") is False or d.get("index_method") == "none"
        if slug in cur:
            if inactive:
                removed.append(slug)
                del cur[slug]
                continue
            for k in ("liked", "target"):
                v = bool(d.get(k))
                if cur[slug].get(k, False) != v:
                    cur[slug][k] = v
                    flagged += 1
            continue
        if inactive:
            continue
        c = detect(d)
        if not c:
            continue
        cur[slug] = {"slug": slug, "name": d.get("name", slug), **c,
                     "liked": bool(d.get("liked")), "target": bool(d.get("target")),
                     "added": dt.date.today().isoformat(),
                     **({"addedBy": d["addedBy"]} if d.get("addedBy") else {})}
        added.append(slug)
    if not (added or removed or flagged):
        print("nincs változás", file=sys.stderr)
        return
    cfg["companies"] = list(cur.values())  # a meglévő sorrend marad, az újak a végére kerülnek
    with open(cfg_path, "w") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"+{len(added)} ({', '.join(added)}) -{len(removed)} ({', '.join(removed)}) jelző: {flagged}",
          file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
