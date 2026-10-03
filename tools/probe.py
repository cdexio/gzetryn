"""Phase 0 probe for GMGN web endpoints (run on the VPS; read-only, polite pace).

Usage:
  probe.py get PATH [PATH ...] [--out DIR] [--gap SEC]   fetch each path, print status/latency/shape, save bodies
  probe.py page URL                                      fetch an HTML page, list its script URLs
  probe.py grep URL [URL ...]                            fetch JS bundles, print API path strings found in them
  probe.py burst PATH --n N --gap SEC                    N requests to one path, report status counts (rate test)

Paths may hold placeholders filled from env: {wallet} {mint} {creator}.
No login, no cookies kept between runs, browser TLS fingerprint (curl_cffi impersonate=chrome).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests

BASE = "https://gmgn.ai"
HEADERS = {"Referer": "https://gmgn.ai/", "Accept": "application/json, text/plain, */*"}


def fill(path: str) -> str:
    for k in ("wallet", "mint", "creator"):
        path = path.replace("{" + k + "}", os.environ.get(k.upper(), ""))
    return path


def shape(v, depth: int = 0) -> str:
    if depth > 2:
        return type(v).__name__
    if isinstance(v, dict):
        inner = ", ".join(f"{k}:{shape(x, depth + 1)}" for k, x in list(v.items())[:60])
        return "{" + inner + "}"
    if isinstance(v, list):
        if not v:
            return "[]"
        return f"[{len(v)}x {shape(v[0], depth + 1)}]"
    if isinstance(v, str):
        return "str"
    return type(v).__name__


def classify(body: str) -> str:
    s = body.lstrip()
    if s.startswith("{") or s.startswith("["):
        return "json"
    if "Just a moment" in body or "challenge-platform" in body:
        return "cf-challenge"
    return "html/other"


def cmd_get(paths: list[str], out: str | None, gap: float) -> None:
    s = requests.Session(impersonate="chrome")
    outdir = Path(out) if out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    for i, raw in enumerate(paths):
        path = fill(raw)
        url = path if path.startswith("http") else BASE + path
        t = time.time()
        try:
            r = s.get(url, timeout=20, headers=HEADERS)
        except Exception as e:  # noqa: BLE001
            print(f"ERR  {path}: {type(e).__name__}: {e}")
            time.sleep(gap)
            continue
        dt = time.time() - t
        body = r.text
        kind = classify(body)
        line = f"{r.status_code} {kind:12} {dt:5.2f}s {len(body):7d}B {path}"
        detail = ""
        if kind == "json":
            try:
                d = json.loads(body)
                if isinstance(d, dict):
                    detail = f"code={d.get('code')!r} msg={str(d.get('msg') or d.get('message'))[:80]!r} data={shape(d.get('data'))}"
                else:
                    detail = shape(d)
            except ValueError:
                detail = "bad json"
        else:
            detail = body[:200].replace("\n", " ")
        print(line)
        print("    " + detail[:3000])
        rl = {k: v for k, v in r.headers.items() if "rate" in k.lower() or k.lower() in ("retry-after", "cf-ray", "server", "cf-cache-status")}
        if rl:
            print(f"    headers={rl}")
        if outdir:
            name = re.sub(r"[^A-Za-z0-9]+", "_", path)[:120] + ".json"
            (outdir / name).write_text(body)
        sys.stdout.flush()
        if i < len(paths) - 1:
            time.sleep(gap)


def cmd_post(path: str, body: str, out: str | None) -> None:
    s = requests.Session(impersonate="chrome")
    path = fill(path)
    t = time.time()
    r = s.post(BASE + path, data=fill(body), timeout=20, headers={**HEADERS, "Content-Type": "application/json"})
    dt = time.time() - t
    kind = classify(r.text)
    print(f"{r.status_code} {kind:12} {dt:5.2f}s {len(r.text):7d}B POST {path} {fill(body)[:200]}")
    if kind == "json":
        d = json.loads(r.text)
        print(f"    code={d.get('code')!r} msg={str(d.get('msg'))[:80]!r} data={shape(d.get('data'))}"[:3000])
    else:
        print("    " + r.text[:200].replace("\n", " "))
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / (re.sub(r"[^A-Za-z0-9]+", "_", "POST" + path)[:120] + ".json")).write_text(r.text)


def cmd_page(url: str) -> None:
    s = requests.Session(impersonate="chrome")
    r = s.get(url, timeout=20, headers={"Accept": "text/html"})
    print(r.status_code, classify(r.text), len(r.text))
    for m in sorted(set(re.findall(r"""(?:src|href)=["']([^"']+\.js[^"']*)["']""", r.text))):
        print(m)


def cmd_grep(urls: list[str], save: str | None = None) -> None:
    s = requests.Session(impersonate="chrome")
    found: set[str] = set()
    if save:
        Path(save).mkdir(parents=True, exist_ok=True)
    for u in urls:
        url = u if u.startswith("http") else BASE + u
        r = s.get(url, timeout=30)
        if save:
            (Path(save) / url.rsplit("/", 1)[-1]).write_text(r.text)
        for m in re.findall(r"""["'`](/(?:api|defi|vas|tapi|sapi|mrwapi|td|pf)/[A-Za-z0-9_/\-{}$.?=&]+)""", r.text):
            found.add(m)
        time.sleep(1.0)
    for f in sorted(found):
        print(f)


def cmd_ctx(directory: str, needles: list[str], before: int, after: int, limit: int) -> None:
    """Print text around each fixed-string needle in saved bundles (fast; no regex backtracking)."""
    texts = {p.name: p.read_text(errors="replace") for p in Path(directory).glob("*.js")}
    for n in needles:
        print(f"=== {n}")
        shown = 0
        for name, t in texts.items():
            i = t.find(n)
            while i >= 0 and shown < limit:
                print(f"[{name}] ...{t[max(0, i - before):i + after]}...")
                shown += 1
                i = t.find(n, i + 1)


def cmd_burst(path: str, n: int, gap: float) -> None:
    s = requests.Session(impersonate="chrome")
    counts: collections.Counter = collections.Counter()
    lat = []
    url = BASE + fill(path)
    t0 = time.time()
    for _ in range(n):
        t = time.time()
        try:
            r = s.get(url, timeout=20, headers=HEADERS)
            counts[f"{r.status_code}:{classify(r.text)}"] += 1
            if r.status_code != 200:
                print("non-200", r.status_code, dict(r.headers))
        except Exception as e:  # noqa: BLE001
            counts[f"ERR:{type(e).__name__}"] += 1
        lat.append(time.time() - t)
        time.sleep(gap)
    lat.sort()
    print(f"n={n} gap={gap}s wall={time.time() - t0:.0f}s counts={dict(counts)} "
          f"p50={lat[len(lat) // 2]:.2f}s p95={lat[int(len(lat) * 0.95) - 1]:.2f}s max={lat[-1]:.2f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("paths", nargs="+")
    g.add_argument("--out")
    g.add_argument("--gap", type=float, default=1.2)
    p = sub.add_parser("page")
    p.add_argument("url")
    gr = sub.add_parser("grep")
    gr.add_argument("urls", nargs="+")
    gr.add_argument("--save", help="also save each bundle into this directory")
    b = sub.add_parser("burst")
    b.add_argument("path")
    b.add_argument("--n", type=int, default=30)
    b.add_argument("--gap", type=float, default=1.0)
    po = sub.add_parser("post")
    po.add_argument("path")
    po.add_argument("body")
    po.add_argument("--out")
    c = sub.add_parser("ctx")
    c.add_argument("dir")
    c.add_argument("needles", nargs="+")
    c.add_argument("--before", type=int, default=200)
    c.add_argument("--after", type=int, default=400)
    c.add_argument("--limit", type=int, default=2)
    a = ap.parse_args()
    if a.cmd == "post":
        cmd_post(a.path, a.body, a.out)
    elif a.cmd == "ctx":
        cmd_ctx(a.dir, a.needles, a.before, a.after, a.limit)
    elif a.cmd == "get":
        cmd_get(a.paths, a.out, a.gap)
    elif a.cmd == "page":
        cmd_page(a.url)
    elif a.cmd == "grep":
        cmd_grep(a.urls, a.save)
    else:
        cmd_burst(a.path, a.n, a.gap)


if __name__ == "__main__":
    main()
