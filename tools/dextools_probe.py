"""DEXTools pair page probe for the `snipers` token part (run on the VPS; read-only).

For rows from gzetryn's candidates (mint, pool_address, GMGN sniper_count when stored) calls
`GET https://www.dextools.io/shared/data/pair?address=<pool>&chain=solana&audit=true&locks=true` (Chrome
impersonation, Referer dextools.io) every --gap seconds and reports: status codes, latency, the response shape, sniper
list length vs GMGN's sniper_count, whether the deployer is a sniper, `promoted`, `migratedFrom`.

Usage: dextools_probe.py --rows rows.json --gap 2 --out dt.json
"""

from __future__ import annotations

import argparse
import collections
import json
import time

from curl_cffi import requests

URL = "https://www.dextools.io/shared/data/pair"
HDR = {"Referer": "https://www.dextools.io/", "Accept": "application/json"}


def shape(v, depth=0):
    if depth > 2:
        return type(v).__name__
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}:{shape(x, depth + 1)}" for k, x in list(v.items())[:40]) + "}"
    if isinstance(v, list):
        return f"[{len(v)}x {shape(v[0], depth + 1)}]" if v else "[]"
    return type(v).__name__


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--gap", type=float, default=2.0)
    ap.add_argument("--out", default="dt.json")
    a = ap.parse_args()
    rows = json.load(open(a.rows))
    s = requests.Session(impersonate="chrome")
    statuses = collections.Counter()
    lat = []
    out = []
    shown = False
    for r in rows:
        t = time.time()
        resp = s.get(URL, params={"address": r["pool_address"], "chain": "solana", "audit": "true", "locks": "true"},
                     headers=HDR, timeout=20)
        lat.append(time.time() - t)
        statuses[resp.status_code] += 1
        item = {**r, "status": resp.status_code}
        try:
            d = resp.json()
        except ValueError:
            item["body"] = resp.text[:120]
            out.append(item)
            time.sleep(a.gap)
            continue
        data = d.get("data") if isinstance(d, dict) else None
        p = data[0] if isinstance(data, list) and data else None
        if p is None:
            item["body"] = str(d)[:200]
        else:
            if not shown:
                print("pair shape:", shape(p)[:2500])
                shown = True
            tok = p.get("token") or {}
            fm = tok.get("firstMakers") or {}
            snipers = fm.get("snipers") or []
            owner = (tok.get("deployment") or {}).get("owner")
            wallets = [x if isinstance(x, str) else (x.get("wallet") or x.get("address") or x.get("maker")) for x in snipers]
            item.update(
                pair_id=(p.get("id") or {}).get("pair") if isinstance(p.get("id"), dict) else p.get("id"),
                token_address=(tok.get("id") or {}).get("token") if isinstance(tok.get("id"), dict) else tok.get("address"),
                exchange=(p.get("exchange") or {}).get("factory") if isinstance(p.get("exchange"), dict) else p.get("exchange"),
                snipers=len(snipers),
                sniper_example=snipers[:2],
                firstMakers_keys=sorted(fm.keys()) if isinstance(fm, dict) else None,
                creator_is_sniper=bool(owner and owner in wallets),
                owner=owner,
                promoted=tok.get("promoted"),
                migratedFrom=p.get("migratedFrom"),
            )
        out.append(item)
        time.sleep(a.gap)
    lat.sort()
    print(f"calls={len(rows)} statuses={dict(statuses)} latency p50={lat[len(lat) // 2]:.2f}s max={lat[-1]:.2f}s")
    for x in out:
        print(x.get("kind"), x.get("source"), x.get("exchange"), "status", x["status"], "snipers", x.get("snipers"),
              "gmgn", x.get("gmgn_sniper_first"), "/", x.get("gmgn_sniper_last"), "creator_sniper", x.get("creator_is_sniper"),
              "promoted", x.get("promoted"), "fm_keys", x.get("firstMakers_keys"), x.get("body", "")[:80])
    json.dump(out, open(a.out, "w"), default=str)


if __name__ == "__main__":
    main()
