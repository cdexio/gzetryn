"""Compare three sources of pump.fun bonding-curve progress on the same clock (run on the VPS; read-only).

1. Chain: `logsSubscribe {mentions: [pump program]}` → TradeEvent (pump IDL, pump-fun/pump-public-docs idl/pump.json)
   decoded in full (incl. mayhem_mode, quote_mint, ix_name).
2. pump.fun frontend API: `GET frontend-api-v3.pump.fun/coins?sort=market_cap&order=DESC&complete=false` polled every
   --poll seconds (records the call status, so its rate limit and freshness can be judged).
3. GMGN `multi_token_info` (mrwapi group): launchpad_progress for curves that were quiet (no trade for 60 s) at the
   end, queried right away → time-aligned comparison with the chain value.

Usage: completing_probe.py --seconds 180 --poll 10 --out probe.json
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
from curl_cffi.requests import AsyncSession

WS = "wss://api.mainnet-beta.solana.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TRADE = hashlib.sha256(b"event:TradeEvent").digest()[:8]
COMPLETE = hashlib.sha256(b"event:CompleteEvent").digest()[:8]
INITIAL_REAL_TOKENS = 793_100_000_000_000  # Global.initial_real_token_reserves (checked on chain separately)
FRONT = "https://frontend-api-v3.pump.fun/coins"
FRONT_HEADERS = {"Origin": "https://pump.fun", "Referer": "https://pump.fun/", "Accept": "application/json"}


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def decode_trade(d: bytes) -> dict:
    o = 8
    mint = b58(d[o : o + 32]); o += 32
    sol, tok = struct.unpack_from("<QQ", d, o); o += 16
    is_buy = d[o] == 1; o += 1
    o += 32  # user
    (ts,) = struct.unpack_from("<q", d, o); o += 8
    vsol, vtok, rsol, rtok = struct.unpack_from("<QQQQ", d, o); o += 32
    o += 32 + 8 + 8 + 32 + 8 + 8  # fee_recipient, fee_bps, fee, creator, creator_fee_bps, creator_fee
    o += 1 + 8 + 8 + 8 + 8  # track_volume, unclaimed, claimed, current_sol_volume, last_update_timestamp
    (n,) = struct.unpack_from("<I", d, o); o += 4
    ix = d[o : o + n].decode("utf-8", "replace"); o += n
    mayhem = None
    quote = None
    if o < len(d):
        mayhem = d[o] == 1; o += 1
        o += 8 + 8 + 8 + 8  # cashback_fee_bps, cashback, buyback_fee_bps, buyback_fee
        if o + 4 <= len(d):
            (k,) = struct.unpack_from("<I", d, o); o += 4
            o += k * 40  # Shareholder {address pubkey, bps u64}? (size assumed; only quote_mint after it)
            if o + 32 <= len(d):
                quote = b58(d[o : o + 32])
    return {"mint": mint, "is_buy": is_buy, "ts": ts, "vsol": vsol, "vtok": vtok, "rsol": rsol, "rtok": rtok,
            "ix": ix, "mayhem": mayhem, "quote": quote, "len": len(d)}


async def chain(seconds: float, state: dict) -> None:
    t0 = time.time()
    async with websockets.connect(WS, max_size=2**23, ping_interval=20) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                  "params": [{"mentions": [PUMP]}, {"commitment": "confirmed"}]}))
        while time.time() - t0 < seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, seconds - (time.time() - t0)))
            except TimeoutError:
                break
            m = json.loads(raw)
            if "params" not in m:
                continue
            v = m["params"]["result"]["value"]
            state["bytes"] += len(raw)
            state["notifications"] += 1
            if v["err"] is not None:
                continue
            for line in v.get("logs") or []:
                if not line.startswith("Program data: "):
                    continue
                try:
                    d = base64.b64decode(line[14:])
                except ValueError:
                    continue
                now = time.time()
                if d[:8] == TRADE:
                    try:
                        ev = decode_trade(d)
                    except struct.error:
                        state["decode_errors"] += 1
                        continue
                    ev["recv"] = now
                    state["trades"] += 1
                    state["last"][ev["mint"]] = ev
                    state["hist"].setdefault(ev["mint"], []).append((now, ev["rtok"]))
                elif d[:8] == COMPLETE:
                    state["complete"].append({"mint": b58(d[8 + 32 : 8 + 64]), "curve": b58(d[8 + 64 : 8 + 96]), "recv": now})


async def frontend(seconds: float, poll: float, state: dict) -> None:
    t0 = time.time()
    async with AsyncSession(impersonate="chrome", timeout=15) as s:
        while time.time() - t0 < seconds:
            t = time.time()
            try:
                r = await s.get(FRONT, params={"offset": 0, "limit": 50, "sort": "market_cap", "order": "DESC",
                                               "includeNsfw": "false", "complete": "false"}, headers=FRONT_HEADERS)
                rows = r.json() if r.status_code == 200 else None
                state["front_calls"].append({"t": t, "status": r.status_code, "latency": time.time() - t,
                                             "rows": rows if isinstance(rows, list) else None})
            except Exception as e:  # noqa: BLE001
                state["front_calls"].append({"t": t, "status": 0, "error": str(e)[:100]})
            await asyncio.sleep(max(0.0, poll - (time.time() - t)))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--poll", type=float, default=10)
    ap.add_argument("--out", default="probe.json")
    a = ap.parse_args()
    st = {"bytes": 0, "notifications": 0, "trades": 0, "decode_errors": 0, "last": {}, "hist": {}, "complete": [],
          "front_calls": []}
    await asyncio.gather(chain(a.seconds, st), frontend(a.seconds, a.poll, st))
    end = time.time()
    # GMGN, time-aligned: standard curves quiet for >= 60 s
    quiet = [m for m, e in st["last"].items() if end - e["recv"] >= 60 and e["vtok"] - e["rtok"] == 279_900_000_000_000]
    quiet.sort(key=lambda m: -(1 - st["last"][m]["rtok"] / INITIAL_REAL_TOKENS))
    quiet = quiet[:20] + quiet[-10:]
    gmgn = {}
    async with AsyncSession(impersonate="chrome", timeout=15) as s:
        for i in range(0, len(quiet), 5):
            r = await s.post("https://gmgn.ai/mrwapi/v1/multi_token_info",
                             data=json.dumps({"chain": "sol", "addresses": quiet[i : i + 5]}),
                             headers={"Referer": "https://gmgn.ai/", "Content-Type": "application/json"})
            for d in (r.json().get("data") or []) if r.status_code == 200 else []:
                gmgn[d["address"]] = {"progress": d.get("launchpad_progress"), "platform": d.get("launchpad_platform")}
            await asyncio.sleep(1.2)
    st["gmgn"] = gmgn
    st["quiet"] = quiet
    st["end"] = end
    json.dump(st, open(a.out, "w"))
    print(f"chain: {st['notifications']} notifications, {st['trades']} trades, {st['bytes'] / a.seconds / 1024:.0f} KiB/s, "
          f"decode_errors={st['decode_errors']}, complete events={len(st['complete'])}, mints={len(st['last'])}")
    calls = st["front_calls"]
    print(f"frontend: {len(calls)} calls, statuses={sorted({c['status'] for c in calls})}, "
          f"latency p50={sorted(c.get('latency', 0) for c in calls)[len(calls) // 2]:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
