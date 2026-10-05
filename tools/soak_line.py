"""Print one soak line from gzetryn's /v1/stats JSON on stdin (used by the phase 8 measurement loop)."""

from __future__ import annotations

import json
import sys
import time


def main() -> None:
    try:
        d = json.load(sys.stdin)["data"]
    except (ValueError, KeyError):
        print(time.strftime("%H:%M:%S"), "no stats (restart?)")
        return
    s = d["since_start"]
    b = d["budget"]
    groups = {
        n: f"thr={g['throttles']} cool={g['cooling_sec']} cd_total={g['cooldown_sec_total']} peak10/60={g['peak']['10']}/{g['peak']['60']}"
        f" before_thr={g['last_throttle_windows']}"
        for n, g in b["groups"].items()
        if g["throttles"] or g["cooling_sec"]
    }
    wa = {k: v for k, v in s["by_endpoint"].get("wallet_activity", {}).items() if k != "avg_latency_ms"}
    src = d["feed_24h"].get("live_by_source", {})
    t = d["watcher"].get("trigger", {})
    print(
        time.strftime("%H:%M:%S"),
        f"up={s['uptime_min']}m rpm={s['gmgn_requests_per_min']} thr={b['throttles']} gpause={b['global_pauses']}",
        f"wa={wa} groups={groups}",
        f"trigger hits={t.get('hits')} timeouts={t.get('timeouts')} polls={t.get('trigger_polls')}",
        f"lag24h={json.dumps(src)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
