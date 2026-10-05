"""Verify on-chain trade decoding against GMGN events (run on the VPS; read-only).

For recent trigger-found trades in gzetryn's feed (`--from-db`) or given signatures, fetch the transaction from the
free public RPC (`getTransaction`, jsonParsed, maxSupportedTransactionVersion 0) and derive, for the wallet:
mint, side, token amount (owner's token balance delta) and SOL amount (lamport delta + wSOL delta, fees excluded).
Prints both versions side by side and the RPC latency.

Usage: chain_decode_probe.py --rows FILE.json   (rows: [{wallet, tx_hash, mint, side, token_amount, sol_amount}])
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"


def rpc(method: str, params: list) -> tuple[dict | None, float]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()), time.time() - t
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}, time.time() - t


def decode(tx: dict, wallet: str) -> list[dict]:
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    idx = keys.index(wallet) if wallet in keys else None
    sol_delta = 0.0
    if idx is not None:
        sol_delta = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9
        if idx == 0:
            sol_delta += meta["fee"] / 1e9  # the fee payer's fee is not part of the trade
    deltas: dict[str, float] = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            amt = float(b["uiTokenAmount"].get("uiAmountString") or 0)
            deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + sign * amt
    wsol = deltas.pop(WSOL, 0.0)
    out = []
    for mint, d in deltas.items():
        if abs(d) < 1e-12:
            continue
        out.append({"mint": mint, "side": "buy" if d > 0 else "sell", "token_amount": abs(d),
                    "sol_amount": abs(sol_delta + wsol)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--gap", type=float, default=2.0)
    a = ap.parse_args()
    rows = json.load(open(a.rows))[: a.n]
    lat = []
    match = 0
    for r in rows:
        res, dt = rpc("getTransaction", [r["tx_hash"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                                                        "commitment": "confirmed"}])
        lat.append(dt)
        tx = (res or {}).get("result")
        if not tx:
            print("NO TX", r["tx_hash"][:12], str(res)[:150])
            time.sleep(a.gap)
            continue
        dec = [x for x in decode(tx, r["wallet"]) if x["mint"] == r["mint"]]
        d = dec[0] if dec else None
        ok = bool(d) and d["side"] == r["side"] and abs(d["token_amount"] - r["token_amount"]) <= 1e-6 * max(1, r["token_amount"])
        sol_ok = bool(d) and r["sol_amount"] is not None and abs(d["sol_amount"] - r["sol_amount"]) <= 0.02 * r["sol_amount"] + 0.001
        match += ok
        print(f"{'OK ' if ok else 'BAD'} sol={'ok' if sol_ok else 'diff'} {r['side']:4} gmgn tok={r['token_amount']:.6g} "
              f"sol={r['sol_amount']} | chain {d and d['side']} tok={d and round(d['token_amount'], 6)} "
              f"sol={d and round(d['sol_amount'], 6)} rpc={dt:.2f}s")
        time.sleep(a.gap)
    lat.sort()
    print(f"matched {match}/{len(rows)}; rpc latency p50={lat[len(lat) // 2]:.2f}s max={lat[-1]:.2f}s")


if __name__ == "__main__":
    main()
