"""Capture real pump.fun log notifications as test fixtures (VPS; read-only).

Keeps the first notification (signature, logs) that contains each wanted event kind: a standard-curve TradeEvent with
progress >= 0.55, a mayhem TradeEvent, a CreateEvent, a CompleteEvent and a CompletePumpAmmMigrationEvent.
Usage: PYTHONPATH=<gzetryn>/src pump_capture.py --seconds 600 --out pump-logs.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import time

import websockets

from gzetryn.trigger.solana import PUMP_PROGRAM, TRADE_EVENT, decode_trade, progress

WS = "wss://api.mainnet-beta.solana.com"
CREATE = hashlib.sha256(b"event:CreateEvent").digest()[:8]
COMPLETE = hashlib.sha256(b"event:CompleteEvent").digest()[:8]
MIGRATION = hashlib.sha256(b"event:CompletePumpAmmMigrationEvent").digest()[:8]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--out", default="pump-logs.json")
    a = ap.parse_args()
    want = {"trade_completing": None, "trade_mayhem": None, "create": None, "complete": None, "migration": None}
    t0 = time.time()
    async with websockets.connect(WS, max_size=2**23, ping_interval=20) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                  "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}))
        while time.time() - t0 < a.seconds and any(v is None for v in want.values()):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except TimeoutError:
                continue
            m = json.loads(raw)
            if "params" not in m:
                continue
            v = m["params"]["result"]["value"]
            if v["err"] is not None:
                continue
            item = {"signature": v["signature"], "slot": m["params"]["result"]["context"]["slot"], "logs": v["logs"]}
            for line in v.get("logs") or []:
                if not line.startswith("Program data: "):
                    continue
                try:
                    d = base64.b64decode(line[14:])
                except ValueError:
                    continue
                if d[:8] == TRADE_EVENT:
                    t = decode_trade(d)
                    if t.mayhem_mode and want["trade_mayhem"] is None:
                        want["trade_mayhem"] = item
                    elif t.standard_curve and t.sol_quote and progress(t.real_token_reserves) >= 0.55 \
                            and want["trade_completing"] is None:
                        want["trade_completing"] = item
                elif d[:8] == CREATE and want["create"] is None:
                    want["create"] = item
                elif d[:8] == COMPLETE and want["complete"] is None:
                    want["complete"] = item
                elif d[:8] == MIGRATION and want["migration"] is None:
                    want["migration"] = item
    json.dump(want, open(a.out, "w"), indent=1)
    print({k: (v is not None) for k, v in want.items()}, f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
