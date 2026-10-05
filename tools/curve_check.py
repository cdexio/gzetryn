"""Ground-truth check for the chain completing reader (run on the VPS; read-only).

1. PDA: derive `bonding-curve` addresses for the mints in a frontend /coins answer and compare with its
   `bonding_curve` field.
2. Accounts: getMultipleAccounts (base64) for curves of mints in a completing_probe output; decode BondingCurve
   (IDL) and compare real_token_reserves / progress with the last TradeEvent value and with GMGN.
3. Frontend sort=last_trade_timestamp: does its reserve value equal the live account at fetch time?

Usage: PYTHONPATH=<gzetryn>/src curve_check.py --probe cprobe.json
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request

from curl_cffi import requests

from gzetryn.trigger.solana import bonding_curve_address, decode_bonding_curve, progress

RPC = "https://api.mainnet-beta.solana.com"
FRONT = "https://frontend-api-v3.pump.fun/coins"
HDR = {"Origin": "https://pump.fun", "Referer": "https://pump.fun/", "Accept": "application/json"}


def rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def accounts(addrs: list[str]) -> dict[str, bytes]:
    out = {}
    for i in range(0, len(addrs), 100):
        res = rpc("getMultipleAccounts", [addrs[i : i + 100], {"encoding": "base64", "commitment": "confirmed"}])
        for a, v in zip(addrs[i : i + 100], res["result"]["value"], strict=True):
            if v:
                out[a] = base64.b64decode(v["data"][0])
        time.sleep(2.5)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", required=True)
    a = ap.parse_args()
    st = json.load(open(a.probe))
    s = requests.Session(impersonate="chrome")

    print("== 1. PDA vs frontend bonding_curve")
    rows = s.get(FRONT, params={"offset": 0, "limit": 50, "sort": "last_trade_timestamp", "order": "DESC",
                                "includeNsfw": "true", "complete": "false"}, headers=HDR, timeout=15).json()
    t_fetch = time.time()
    ok = sum(1 for r in rows if bonding_curve_address(r["mint"]) == r["bonding_curve"])
    print(f"PDA matches {ok}/{len(rows)}")

    print("== 3. frontend (sort=last_trade_timestamp) vs live account")
    curves = {r["mint"]: r["bonding_curve"] for r in rows}
    acc = accounts(list(curves.values()))
    t_acc = time.time()
    same = diff = 0
    lags = []
    for r in rows:
        bc = decode_bonding_curve(acc.get(r["bonding_curve"], b""))
        if bc is None:
            continue
        if int(r["real_token_reserves"]) == bc.real_token_reserves:
            same += 1
        else:
            diff += 1
        lags.append(t_fetch - r["last_trade_timestamp"] / 1000 if r["last_trade_timestamp"] > 1e12 else t_fetch - r["last_trade_timestamp"])
    lags.sort()
    print(f"frontend reserves == account (read {t_acc - t_fetch:.1f}s later): same={same} differ={diff}; "
          f"newest last_trade age p50={lags[len(lags) // 2]:.1f}s min={lags[0]:.1f}s max={lags[-1]:.1f}s")

    print("== 2. account vs last TradeEvent vs GMGN (probe's quiet curves)")
    quiet = st["quiet"]
    qa = accounts([bonding_curve_address(m) for m in quiet])
    exact_ev = exact_g = n = 0
    for m in quiet:
        bc = decode_bonding_curve(qa.get(bonding_curve_address(m), b""))
        if bc is None:
            continue
        n += 1
        ev = st["last"][m]
        g = (st["gmgn"].get(m) or {}).get("progress")
        same_ev = bc.real_token_reserves == ev["rtok"]
        exact_ev += same_ev
        if g is not None and not bc.is_mayhem_mode and abs(float(g) - progress(bc.real_token_reserves)) < 1e-4:
            exact_g += 1
        print(f"{m[:8]} mayhem={bc.is_mayhem_mode} complete={bc.complete} account_progress={progress(bc.real_token_reserves):.4f} "
              f"event_rtok_equal={same_ev} gmgn={g}")
    print(f"accounts decoded={n}; equal to last TradeEvent={exact_ev}; GMGN exact (non-mayhem)={exact_g}")


if __name__ == "__main__":
    main()
