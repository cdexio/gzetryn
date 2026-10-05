"""Raydium LaunchLab list API: rate limit and freshness vs the chain (run on the VPS; read-only).

1. Burst: N calls at a short gap → status counts.
2. Poll `get/list?sort=new` every --poll seconds for --seconds; record when each mint is first seen.
3. For up to --check new mints: the pool's creation block time = blockTime of the oldest signature of the pool
   account (getSignaturesForAddress on the free public RPC, paced 2.5 s) → lag = first seen in the API − block time;
   also createAt − block time.

Usage: launchlab_probe.py --seconds 600 --poll 5 --check 15 --out ll_probe.json
"""

from __future__ import annotations

import argparse
import collections
import json
import time
import urllib.request

from curl_cffi import requests

API = "https://launch-mint-v1.raydium.io/get/list"
RPC = "https://api.mainnet-beta.solana.com"


def rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def oldest_block_time(address: str) -> int | None:
    before = None
    for _ in range(5):
        params = {"limit": 1000}
        if before:
            params["before"] = before
        res = rpc("getSignaturesForAddress", [address, params]).get("result") or []
        time.sleep(2.5)
        if not res:
            return None
        if len(res) < 1000:
            return res[-1].get("blockTime")
        before = res[-1]["signature"]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--poll", type=float, default=5)
    ap.add_argument("--burst", type=int, default=20)
    ap.add_argument("--check", type=int, default=15)
    ap.add_argument("--out", default="ll_probe.json")
    a = ap.parse_args()
    s = requests.Session(impersonate="chrome")
    params = {"sort": "new", "size": 50, "mintType": "default", "includeNsfw": "false"}

    burst = collections.Counter()
    for _ in range(a.burst):
        r = s.get(API, params=params, timeout=15)
        burst[r.status_code] += 1
        time.sleep(0.3)
    print(f"burst {a.burst} calls at 0.3 s: {dict(burst)}")

    first: dict[str, dict] = {}
    statuses = collections.Counter()
    lat = []
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        t = time.time()
        try:
            r = s.get(API, params=params, timeout=15)
            statuses[r.status_code] += 1
            lat.append(time.time() - t)
            rows = (r.json().get("data") or {}).get("rows") or [] if r.status_code == 200 else []
        except Exception as e:  # noqa: BLE001
            statuses[f"err:{type(e).__name__}"] += 1
            rows = []
        for x in rows:
            if x["mint"] not in first:
                first[x["mint"]] = {"seen": t, "createAt": x["createAt"] / 1000, "pool": x["poolId"],
                                    "platform": (x.get("platformInfo") or {}).get("name"), "initial": t - t0 < 1}
        time.sleep(max(0.0, a.poll - (time.time() - t)))
    lat.sort()
    new = {m: v for m, v in first.items() if not v["initial"]}
    print(f"poll {sum(statuses.values())} calls every {a.poll}s: {dict(statuses)}; latency p50={lat[len(lat) // 2]:.2f}s")
    print(f"mints appearing during the window: {len(new)} "
          f"({len(new) / (a.seconds / 3600):.0f}/h); platforms {collections.Counter(v['platform'] for v in new.values()).most_common(5)}")
    seen_minus_create = sorted(v["seen"] - v["createAt"] for v in new.values())
    if seen_minus_create:
        print(f"first seen − createAt: p50={seen_minus_create[len(seen_minus_create) // 2]:.1f}s "
              f"p90={seen_minus_create[int(len(seen_minus_create) * .9)]:.1f}s")
    checks = []
    for m, v in list(new.items())[: a.check]:
        bt = oldest_block_time(v["pool"])
        if bt:
            checks.append((v["seen"] - bt, v["createAt"] - bt, m, v["platform"]))
    for c in checks:
        print(f"{c[2][:8]} {c[3]}: api_first_seen − block={c[0]:.1f}s createAt − block={c[1]:.1f}s")
    if checks:
        lags = sorted(c[0] for c in checks)
        print(f"API lag vs chain: p50={lags[len(lags) // 2]:.1f}s max={lags[-1]:.1f}s (poll every {a.poll}s)")
    json.dump({"first": first, "checks": checks, "statuses": dict(statuses), "burst": dict(burst)}, open(a.out, "w"))


if __name__ == "__main__":
    main()
