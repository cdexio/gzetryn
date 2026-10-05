"""Check pump.fun CreateEvent / CompleteEvent / CompletePumpAmmMigrationEvent in the program's logs (VPS; read-only).

Layouts: pump IDL (pump-fun/pump-public-docs idl/pump.json). Prints decoded examples and, per mint, the delay from
CompleteEvent to CompletePumpAmmMigrationEvent; saves migrations (mint, pool) to --out for a pool cross-check.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import struct
import time

import websockets

WS = "wss://api.mainnet-beta.solana.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
D = {hashlib.sha256(f"event:{n}".encode()).digest()[:8]: n for n in ("CreateEvent", "CompleteEvent", "CompletePumpAmmMigrationEvent")}


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def string(d: bytes, o: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", d, o)
    return d[o + 4 : o + 4 + n].decode("utf-8", "replace"), o + 4 + n


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--out", default="migrations.json")
    a = ap.parse_args()
    t0 = time.time()
    creates = 0
    create_examples = []
    complete: dict[str, float] = {}
    migr = []
    async with websockets.connect(WS, max_size=2**23, ping_interval=20) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                  "params": [{"mentions": [PUMP]}, {"commitment": "confirmed"}]}))
        while time.time() - t0 < a.seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, a.seconds - (time.time() - t0)))
            except TimeoutError:
                break
            m = json.loads(raw)
            if "params" not in m:
                continue
            v = m["params"]["result"]["value"]
            if v["err"] is not None:
                continue
            for line in v.get("logs") or []:
                if not line.startswith("Program data: "):
                    continue
                try:
                    d = base64.b64decode(line[14:])
                except ValueError:
                    continue
                name = D.get(d[:8])
                now = time.time()
                if name == "CreateEvent":
                    creates += 1
                    nm, o = string(d, 8)
                    sym, o = string(d, o)
                    _, o = string(d, o)
                    mint = b58(d[o : o + 32])
                    if len(create_examples) < 3:
                        create_examples.append((nm[:30], sym[:15], mint))
                elif name == "CompleteEvent":
                    complete[b58(d[8 + 32 : 8 + 64])] = now
                elif name == "CompletePumpAmmMigrationEvent":
                    o = 8 + 32
                    mint = b58(d[o : o + 32]); o += 32
                    o += 8 + 8 + 8  # mint_amount, sol_amount, pool_migration_fee
                    curve = b58(d[o : o + 32]); o += 32
                    (ts,) = struct.unpack_from("<q", d, o); o += 8
                    pool = b58(d[o : o + 32])
                    migr.append({"mint": mint, "curve": curve, "pool": pool, "ts": ts, "recv": now, "sig": v["signature"],
                                 "after_complete_sec": round(now - complete[mint], 2) if mint in complete else None})
    print(f"seconds={a.seconds:.0f} creates={creates} completes={len(complete)} migrations={len(migr)}")
    print("create examples:", create_examples)
    for x in migr:
        print("migration", x["mint"][:8], "pool", x["pool"], "after_complete_sec", x["after_complete_sec"], "lag_vs_ts", round(x["recv"] - x["ts"], 1))
    json.dump(migr, open(a.out, "w"))


if __name__ == "__main__":
    asyncio.run(main())
