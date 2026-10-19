#!/usr/bin/env python3
"""Előrejelzés: mekkora eséllyel ír ki egy cég új állást a következő 30 / 90 napban.

A gyűjtő napi kimenetéből (out/jobs.json, out/report.json) egy tartós előzményt épít
(out/history.json), és cégenként becsli a hirdetési ütemet:

  * Megfigyelt új hirdetések: egy hirdetés akkor számít újnak, ha a cég első sikeres
    gyűjtési napja UTÁN jelent meg, és a cég előző gyűjtése is sikeres volt legfeljebb 3
    nappal korábban. Így a kezdő „alaplista” és egy kimaradt nap utáni visszatérés nem
    számít új hirdetésnek.
  * Visszamenőleges becslés: ahol az ATS közli a feladás dátumát (a hirdetések legalább
    60 %-ánál), a cég első gyűjtési napja előtti 30 nap feladásai is beszámítanak. Ez
    alulbecsül (a már lezárt hirdetések nem látszanak), ezért csak indításnak jó.
  * Gamma–Poisson (empirikus Bayes) becslés: kevés adatnál a cég a hasonló méretű cégek
    átlaga felé húzódik; ahogy gyűlik az adat, a saját ütemét mutatja.
  * Releváns pozíció: a marketing / digitális / CRM / CX / AI területű, vezetői című
    hirdetések aránya, szintén az átlag felé húzva.

P(legalább 1 új hirdetés T napon belül) = 1 − exp(−λ·T).

Visszamérés: hetente elmenti a jóslatokat (out/forecast_log.json), és 30 nap után
összeveti a valósággal (Brier-pont, kalibráció), az eredmény a forecast.json `eval`
mezőjébe kerül.

Használat: python3 forecast.py [--out out] [--bootstrap-git]
  --bootstrap-git: az előzményt a git-történet korábbi napi pillanatképeiből tölti fel
  (egyszer kell, az első futáskor).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import subprocess
from collections import defaultdict
from pathlib import Path

REL_AREA = re.compile(
    r"marketing|brand|márka|marka|kommunik|communicat|\bpr\b|digital|digitál|digital|growth|performance|"
    r"e-?commerce|webshop|omnichannel|crm|loyalty|lifecycle|martech|customer experience|\bcx\b|"
    r"ügyfélélmény|ugyfelelmeny|customer journey|product marketing|\bai\b|artificial intelligence|"
    r"transform|media|média|content|social", re.I)
REL_LEAD = re.compile(
    r"head|director|igazgat|vezet|\blead\b|leader|chief|\bcmo\b|\bcdo\b|\bvp\b|vice president|"
    r"manager|menedzser|owner|chapter|csoportvezet|osztályvezet|főosztály", re.I)
REL_EXCL = re.compile(r"intern|gyakornok|junior|trainee|assistant|asszisztens|sales representative|"
                      r"értékesítő|developer|fejlesztő|engineer|mérnök", re.I)

HU_MON = {"jan": 1, "febr": 2, "feb": 2, "márc": 3, "marc": 3, "ápr": 4, "apr": 4, "máj": 5, "maj": 5,
          "jún": 6, "jun": 6, "júl": 7, "jul": 7, "aug": 8, "szept": 9, "szep": 9, "okt": 10,
          "nov": 11, "dec": 12}
EN_MON = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                          "sep", "oct", "nov", "dec"])}
BACKFILL_DAYS = 30
MAX_GAP = 3            # ennél több nap kimaradás után újraindul a megfigyelés
PRIOR_DAYS = 45        # a prior „súlya” napokban
SHARE_PRIOR = 25       # a releváns arány prior-súlya hirdetésben


def is_rel(title: str) -> bool:
    t = title or ""
    return bool(REL_AREA.search(t) and REL_LEAD.search(t) and not REL_EXCL.search(t))


def parse_posted(raw: str, seen: dt.date) -> tuple[dt.date | None, bool]:
    """(dátum, ismert-e). A „30+ napja” ismert, de régebbi a visszamenőleges ablaknál."""
    s = " ".join((raw or "").split()).strip()
    if not s:
        return None, False
    if m := re.match(r"(\d{4})-(\d{2})-(\d{2})", s):
        return dt.date(int(m[1]), int(m[2]), int(m[3])), True
    if m := re.search(r"(\d+)\+\s*(napja|days? ago)", s, re.I):
        return seen - dt.timedelta(days=int(m[1]) + 1), True
    if m := re.search(r"(\d+)\s*(napja|days? ago)", s, re.I):
        return seen - dt.timedelta(days=int(m[1])), True
    if re.search(r"today|ma\b|tegnap|yesterday", s, re.I):
        return seen - dt.timedelta(days=1 if re.search(r"tegnap|yesterday", s, re.I) else 0), True
    if m := re.match(r"(\d{4})\.\s*([a-záéíóöőúüű]+)\.?\s*(\d{1,2})", s, re.I):
        mon = next((v for k, v in HU_MON.items() if m[2].lower().startswith(k)), None)
        if mon:
            return dt.date(int(m[1]), mon, int(m[3])), True
    if m := re.match(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", s):
        mon = EN_MON.get(m[2][:3].lower())
        if mon:
            return dt.date(int(m[3]), mon, int(m[1])), True
    if m := re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})", s):
        mon = EN_MON.get(m[1][:3].lower())
        if mon:
            return dt.date(int(m[3]), mon, int(m[2])), True
    return None, False


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def merge_snapshot(hist: dict, jobs: list[dict], report: dict):
    day = report["generatedAt"][:10]
    seen = d(day)
    names = {r["slug"]: r["company"] for r in report.get("companies", [])}
    ok = {r["slug"] for r in report.get("companies", []) if r.get("status") == "ok"}
    for slug in names:
        c = hist["companies"].setdefault(slug, {"name": names[slug], "okDays": []})
        c["name"] = names[slug]
        if slug in ok and day not in c["okDays"]:
            c["okDays"].append(day)
            c["okDays"].sort()
    for j in jobs:
        if j["slug"] not in ok:
            continue
        a = hist["ads"].get(j["url"])
        if not a:
            pd, known = parse_posted(j.get("postedAt", ""), seen)
            a = hist["ads"][j["url"]] = {
                "slug": j["slug"], "title": (j.get("title") or "")[:200], "firstSeen": day,
                "posted": pd.isoformat() if pd else None, "postedKnown": known, "rel": is_rel(j.get("title", ""))}
        a["lastSeen"] = day
    hist["days"] = sorted(set(hist.get("days", [])) | {day})


def bootstrap_git(hist: dict, out: Path):
    rel = out.as_posix()
    log = subprocess.run(["git", "log", "--reverse", "--format=%H", "--", f"{rel}/jobs.json"],
                         capture_output=True, text=True, check=True).stdout.split()
    for h in log:
        try:
            jobs = json.loads(subprocess.run(["git", "show", f"{h}:./{rel}/jobs.json"], capture_output=True,
                                             text=True, check=True).stdout)["jobs"]
            rep = json.loads(subprocess.run(["git", "show", f"{h}:./{rel}/report.json"], capture_output=True,
                                            text=True, check=True).stdout)
        except Exception:  # noqa: BLE001
            continue
        merge_snapshot(hist, jobs, rep)


def observed(c: dict) -> tuple[list[tuple[dt.date, dt.date]], dt.date | None]:
    """Folytonos megfigyelési szakaszok (előző ok-nap, ok-nap] és az első ok-nap."""
    days = [d(x) for x in c["okDays"]]
    if not days:
        return [], None
    segs = [(a, b) for a, b in zip(days, days[1:]) if 0 < (b - a).days <= MAX_GAP]
    return segs, days[0]


def compute(hist: dict, today: dt.date, meta: dict | None = None) -> dict:
    meta = meta or {}
    by_co = defaultdict(list)
    for a in hist["ads"].values():
        by_co[a["slug"]].append(a)
    stats = {}
    for slug, c in hist["companies"].items():
        ads = by_co.get(slug, [])
        segs, first = observed(c)
        if first is None:
            continue
        expo = sum((b - a).days for a, b in segs)
        k = sum(1 for a in ads if any(s < d(a["firstSeen"]) <= e for s, e in segs))
        k_rel = sum(1 for a in ads if a["rel"] and any(s < d(a["firstSeen"]) <= e for s, e in segs))
        # visszamenőleges ablak, ha az ATS közli a dátumot
        base = [a for a in ads if a["firstSeen"] == first.isoformat()]
        known = sum(1 for a in base if a["postedKnown"])
        bf = bf_rel = 0
        if base and known / len(base) >= 0.6:
            win0 = first - dt.timedelta(days=BACKFILL_DAYS)
            for a in base:
                if a["posted"] and win0 <= d(a["posted"]) < first:
                    bf += 1
                    bf_rel += a["rel"]
            expo += BACKFILL_DAYS
            k += bf
            k_rel += bf_rel
        open_now = [a for a in ads if a.get("lastSeen") == max(c["okDays"])]
        rel_ads = [a for a in ads if a["rel"]]
        stats[slug] = {"name": c["name"], "expo": expo, "k": k, "kRel": k_rel, "backfill": bf,
                       "open": len(open_now), "openRel": sum(a["rel"] for a in open_now),
                       "seenAds": len(ads), "seenRel": len(rel_ads),
                       "lastRel": max((a.get("posted") or a["firstSeen"] for a in rel_ads), default=None),
                       "lastOk": max(c["okDays"])}
    # populációs paraméterek: hirdetési ütem nyitott állásonként (méret szerinti prior),
    # szegmensenként (pl. „allami”), ha a szegmensben elég cég van; különben a teljes minta
    def params(group):
        tk = sum(s["k"] for s in group)
        te = sum(s["expo"] * (s["open"] + 1) for s in group) or 1
        sh = (sum(s["seenRel"] for s in group) + 1) / (sum(s["seenAds"] for s in group) + 20)
        return tk / te, sh
    rate_per_open, g_share = params(list(stats.values()))
    seg_of = {k: (meta.get(k) or {}).get("segment") or "" for k in stats}
    seg_params = {}
    for seg in {v for v in seg_of.values() if v}:
        grp = [stats[k] for k in stats if seg_of[k] == seg]
        if len(grp) >= 8:
            seg_params[seg] = params(grp)
    out = []
    for slug, s in stats.items():
        seg = seg_of.get(slug, "")
        r_open, sh0 = seg_params.get(seg, (rate_per_open, g_share))
        mu = r_open * (s["open"] + 1)                             # a cég prior napi üteme
        lam = (mu * PRIOR_DAYS + s["k"]) / (PRIOR_DAYS + s["expo"])
        share = (SHARE_PRIOR * sh0 + s["seenRel"]) / (SHARE_PRIOR + s["seenAds"])
        lam_rel = lam * share
        p = lambda l, t: 1 - math.exp(-l * t)  # noqa: E731
        conf = "magas" if s["expo"] >= 90 else "közepes" if s["expo"] >= 30 else "alacsony"
        drivers = []
        if s["k"]:
            drivers.append(f"{s['k']} új hirdetés {s['expo']} megfigyelt nap alatt"
                           + (f" (ebből {s['backfill']} a feladási dátumokból)" if s["backfill"] else ""))
        else:
            drivers.append(f"{s['expo']} megfigyelt nap alatt nem jelent meg új hirdetés")
        drivers.append(f"most {s['open']} nyitott állás, ebből {s['openRel']} releváns")
        if s["seenRel"]:
            drivers.append(f"a látott hirdetések {round(100 * s['seenRel'] / max(1, s['seenAds']))}%-a releváns")
        out.append({
            "slug": slug, "name": s["name"], "segment": seg, "watchOnly": bool((meta.get(slug) or {}).get("watchOnly")),
            "open": s["open"], "openRel": s["openRel"],
            "newObserved": s["k"], "newRelObserved": s["kRel"], "exposureDays": s["expo"],
            "ratePerMonth": round(lam * 30, 2), "relRatePerMonth": round(lam_rel * 30, 3),
            "pAny30": round(p(lam, 30), 3), "pRel30": round(p(lam_rel, 30), 3), "pRel90": round(p(lam_rel, 90), 3),
            "relShare": round(share, 3), "lastRel": s["lastRel"], "confidence": conf, "drivers": drivers,
        })
    out.sort(key=lambda x: -x["pRel30"])
    return {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "asOf": today.isoformat(),
            "method": "Gamma–Poisson ütembecslés cégenként, méret szerinti priorral; releváns = "
                      "marketing/digitális/CRM/CX/AI terület + vezetői cím. P = 1 − e^(−λT).",
            "population": {"companies": len(out), "ratePerOpenPerMonth": round(rate_per_open * 30, 4),
                           "relShare": round(g_share, 4), "days": len(hist.get("days", [])),
                           "segments": {k: {"companies": sum(1 for x in seg_of.values() if x == k),
                                            "ratePerOpenPerMonth": round(v[0] * 30, 4), "relShare": round(v[1], 4)}
                                        for k, v in seg_params.items()}},
            "companies": out}


def evaluate(log: list[dict], hist: dict, today: dt.date) -> dict:
    """A legalább 30 napos jóslatok visszamérése: Brier-pont és kalibráció (10%-os sávok)."""
    by = defaultdict(list)
    for a in hist["ads"].values():
        by[a["slug"]].append(a)
    rows = []
    for snap in log:
        t0 = d(snap["date"])
        if (today - t0).days < 30:
            continue
        for slug, pr in snap["p"].items():
            hit = any(a["rel"] and t0 < d(a["firstSeen"]) <= t0 + dt.timedelta(days=30) for a in by.get(slug, []))
            rows.append((pr, 1 if hit else 0))
    if not rows:
        return {"status": "még nincs 30 napos jóslat", "n": 0}
    brier = sum((p - y) ** 2 for p, y in rows) / len(rows)
    base = sum(y for _, y in rows) / len(rows)
    bins = defaultdict(lambda: [0, 0, 0.0])
    for p, y in rows:
        b = min(9, int(p * 10))
        bins[b][0] += 1
        bins[b][1] += y
        bins[b][2] += p
    return {"status": "ok", "n": len(rows), "brier": round(brier, 4),
            "brierBaseline": round(base * (1 - base), 4), "hitRate": round(base, 3),
            "calibration": [{"bin": f"{b * 10}-{b * 10 + 10}%", "n": v[0], "predicted": round(v[2] / v[0], 3),
                             "observed": round(v[1] / v[0], 3)} for b, v in sorted(bins.items())]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--bootstrap-git", action="store_true")
    ap.add_argument("--config", default="companies.json")
    a = ap.parse_args()
    out = Path(a.out)
    hist = load(out / "history.json", {"companies": {}, "ads": {}, "days": []})
    if a.bootstrap_git:
        bootstrap_git(hist, out)
    jobs = load(out / "jobs.json", {"jobs": []})["jobs"]
    rep = load(out / "report.json", None)
    if rep:
        merge_snapshot(hist, jobs, rep)
    today = d(rep["generatedAt"]) if rep else dt.date.today()
    cfg = load(Path(a.config), {"companies": []})
    fc = compute(hist, today, {c["slug"]: c for c in cfg.get("companies", [])})
    log = load(out / "forecast_log.json", [])
    if not log or (today - d(log[-1]["date"])).days >= 7:
        log.append({"date": today.isoformat(), "p": {c["slug"]: c["pRel30"] for c in fc["companies"]}})
        log = log[-60:]
    fc["eval"] = evaluate(log, hist, today)
    (out / "history.json").write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")))
    (out / "forecast_log.json").write_text(json.dumps(log, ensure_ascii=False, separators=(",", ":")))
    (out / "forecast.json").write_text(json.dumps(fc, ensure_ascii=False, indent=1))
    top = fc["companies"][:5]
    print(json.dumps({"companies": len(fc["companies"]), "days": fc["population"]["days"],
                      "top": [(c["name"], c["pRel30"]) for c in top]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
