"""Probe a Solana WebSocket for wallet triggers (run on the VPS; read-only).

For every wallet: one `logsSubscribe` with `mentions: [wallet]`, on one connection per commitment (processed and
confirmed in parallel). Records every notification with its receive time; at the end fetches the block time of the
notified slots (getBlockTime over HTTP, polite pace) and reports delay = receive − block time per commitment, the
processed→confirmed gap for the same signature, subscription acks/errors and disconnects.

Usage: ws_probe.py --wallets FILE --minutes 10 [--ws URL] [--http URL] [--out FILE.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.request

import websockets

WS = "wss://api.mainnet-beta.solana.com"
HTTP = "https://api.mainnet-beta.solana.com"


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2)


async def run_conn(url: str, commitment: str, wallets: list[str], until: float, rec: list, st: dict) -> None:
    while time.time() < until:
        try:
            async with websockets.connect(url, max_size=2**22, ping_interval=20, ping_timeout=20) as ws:
                st["connects"] += 1
                subs: dict[int, str] = {}
                pending: dict[int, str] = {}
                t0 = time.time()
                for n, w in enumerate(wallets, start=1):
                    pending[n] = w
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": n, "method": "logsSubscribe",
                        "params": [{"mentions": [w]}, {"commitment": commitment}],
                    }))
                while time.time() < until:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, until - time.time()))
                    except TimeoutError:
                        break
                    now = time.time()
                    m = json.loads(raw)
                    if "id" in m:
                        w = pending.pop(m["id"], None)
                        if "result" in m:
                            subs[m["result"]] = w
                            st["acks"] += 1
                            if not pending:
                                st["subscribe_all_sec"] = round(now - t0, 2)
                        else:
                            st["sub_errors"].append(str(m.get("error"))[:200])
                        continue
                    if m.get("method") == "logsNotification":
                        p = m["params"]
                        v = p["result"]["value"]
                        rec.append({
                            "c": commitment, "wallet": subs.get(p["subscription"]), "sig": v["signature"],
                            "err": v["err"] is not None, "slot": p["result"]["context"]["slot"], "t": now,
                            "nlogs": len(v.get("logs") or []),
                        })
                        st["bytes"] += len(raw)
        except Exception as e:  # noqa: BLE001
            st["disconnects"].append(f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {str(e)[:150]}")
            await asyncio.sleep(2)


def block_times(http: str, slots: list[int]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for s in slots:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getBlockTime", "params": [s]}).encode()
        req = urllib.request.Request(http, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                out[s] = json.loads(r.read()).get("result")
        except Exception:  # noqa: BLE001
            out[s] = None
        time.sleep(0.25)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", required=True)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--ws", default=WS)
    ap.add_argument("--http", default=HTTP)
    ap.add_argument("--out", default=None)
    ap.add_argument("--commitments", default="processed,confirmed")
    a = ap.parse_args()
    wallets = [w.strip() for w in open(a.wallets) if w.strip()]
    until = time.time() + a.minutes * 60
    rec: list = []
    stats = {}
    tasks = []
    for c in a.commitments.split(","):
        stats[c] = {"connects": 0, "acks": 0, "sub_errors": [], "disconnects": [], "bytes": 0}
        tasks.append(run_conn(a.ws, c, wallets, until, rec, stats[c]))
    await asyncio.gather(*tasks)
    if a.out:
        with open(a.out, "w") as f:
            for r in rec:
                f.write(json.dumps(r) + "\n")
    slots = sorted({r["slot"] for r in rec if not r["err"]})
    bt = block_times(a.http, slots[:400])
    print(f"wallets={len(wallets)} minutes={a.minutes} ws={a.ws}")
    for c, st in stats.items():
        rs = [r for r in rec if r["c"] == c]
        ok = [r for r in rs if not r["err"]]
        d = [r["t"] - bt[r["slot"]] for r in ok if bt.get(r["slot"])]
        print(f"[{c}] connects={st['connects']} acks={st['acks']}/{len(wallets)} subscribe_all={st.get('subscribe_all_sec')}s "
              f"notifications={len(rs)} ok={len(ok)} err={len(rs) - len(ok)} wallets_seen={len({r['wallet'] for r in rs})} "
              f"bytes={st['bytes']} delay_vs_blocktime n={len(d)} p50={pct(d, .5)} p90={pct(d, .9)} max={pct(d, 1)}")
        if st["sub_errors"]:
            print(f"  sub_errors({len(st['sub_errors'])}): {st['sub_errors'][:3]}")
        if st["disconnects"]:
            print(f"  disconnects({len(st['disconnects'])}): {st['disconnects'][:5]}")
    p = {r["sig"]: r["t"] for r in rec if r["c"] == "processed"}
    cf = {r["sig"]: r["t"] for r in rec if r["c"] == "confirmed"}
    both = [cf[s] - p[s] for s in p.keys() & cf.keys()]
    if both:
        print(f"confirmed - processed (same sig) n={len(both)} p50={pct(both, .5)} p90={pct(both, .9)} "
              f"mean={round(statistics.mean(both), 2)}; only_processed={len(p.keys() - cf.keys())} "
              f"only_confirmed={len(cf.keys() - p.keys())}")


if __name__ == "__main__":
    asyncio.run(main())
