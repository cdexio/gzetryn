"""Probe the pump.fun program's log stream on a Solana WebSocket (run on the VPS; read-only).

Subscribes `logsSubscribe {mentions: [pump program]}` for N seconds and reports: notifications, bytes, failed vs ok
transactions, "Program data:" lines, and how many decode as Anchor events of the pump program (discriminator =
sha256("event:<Name>")[:8]) for TradeEvent / CreateEvent / CompleteEvent. Decoded TradeEvents (mint, reserves) are
written to --out as JSON lines so progress can be compared with GMGN afterwards.

Usage: pump_probe.py --seconds 60 [--commitment confirmed] [--out trades.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import hashlib
import json
import struct
import time

import websockets

WS = "wss://api.mainnet-beta.solana.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


EVENTS = {disc(n): n for n in ("TradeEvent", "CreateEvent", "CompleteEvent", "SetParamsEvent", "CollectCreatorFeeEvent")}


def decode_trade(d: bytes) -> dict:
    # TradeEvent (pump IDL): mint pubkey, sol_amount u64, token_amount u64, is_buy bool, user pubkey, timestamp i64,
    # virtual_sol_reserves u64, virtual_token_reserves u64, real_sol_reserves u64, real_token_reserves u64, ...
    o = 8
    mint = b58(d[o : o + 32]); o += 32
    sol, tok = struct.unpack_from("<QQ", d, o); o += 16
    is_buy = d[o] == 1; o += 1
    user = b58(d[o : o + 32]); o += 32
    (ts,) = struct.unpack_from("<q", d, o); o += 8
    vsol, vtok, rsol, rtok = struct.unpack_from("<QQQQ", d, o); o += 32
    return {"mint": mint, "sol": sol, "tok": tok, "is_buy": is_buy, "user": user, "ts": ts,
            "vsol": vsol, "vtok": vtok, "rsol": rsol, "rtok": rtok, "len": len(d)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--commitment", default="confirmed")
    ap.add_argument("--ws", default=WS)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    c = collections.Counter()
    sizes: list[int] = []
    out = open(a.out, "w") if a.out else None
    mints: set[str] = set()
    lens = collections.Counter()
    t0 = time.time()
    async with websockets.connect(a.ws, max_size=2**23, ping_interval=20) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                  "params": [{"mentions": [PUMP]}, {"commitment": a.commitment}]}))
        while time.time() - t0 < a.seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, a.seconds - (time.time() - t0)))
            except TimeoutError:
                break
            m = json.loads(raw)
            if "id" in m:
                c["ack" if "result" in m else "sub_error"] += 1
                if "error" in m:
                    print("sub error", m["error"])
                continue
            v = m["params"]["result"]["value"]
            c["notifications"] += 1
            sizes.append(len(raw))
            if v["err"] is not None:
                c["failed_tx"] += 1
                continue
            c["ok_tx"] += 1
            logs = v.get("logs") or []
            if any("Log truncated" in x for x in logs):
                c["truncated_logs"] += 1
            for line in logs:
                if not line.startswith("Program data: "):
                    continue
                c["program_data"] += 1
                try:
                    d = base64.b64decode(line[14:])
                except ValueError:
                    continue
                name = EVENTS.get(d[:8])
                if not name:
                    c["data_other"] += 1
                    continue
                c[name] += 1
                if name == "TradeEvent":
                    lens[len(d)] += 1
                    ev = decode_trade(d)
                    mints.add(ev["mint"])
                    if out:
                        out.write(json.dumps({**ev, "sig": v["signature"], "recv": time.time()}) + "\n")
    dt = time.time() - t0
    sizes.sort()
    print(f"seconds={dt:.0f} commitment={a.commitment} counts={dict(c)}")
    print(f"per_sec: notifications={c['notifications'] / dt:.1f} trades={c['TradeEvent'] / dt:.1f} "
          f"bytes={sum(sizes) / dt / 1024:.0f} KiB/s ({sum(sizes) / 1e6 * 30 / dt:.1f} MB per 30 s); "
          f"msg size p50={sizes[len(sizes) // 2] if sizes else 0} p95={sizes[int(len(sizes) * .95)] if sizes else 0}")
    print(f"distinct mints traded={len(mints)} TradeEvent byte lengths={dict(lens)}")


if __name__ == "__main__":
    asyncio.run(main())
