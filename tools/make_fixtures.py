"""Trim saved phase 0 answers (tools/probe.py --out) into small test fixtures.

Usage: make_fixtures.py SRC_DIR... --dest DIR
Each known answer is matched by a substring of its saved file name; long lists are cut to a few items so the
fixtures stay small but keep GMGN's exact field names and value types.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# (substring of the saved file name, fixture name, dotted path of the list to trim, items kept)
RULES = [
    ("rank_sol_wallets_7d_tag_kol", "rank-wallets-kol-7d.json", "data.rank", 6),
    ("rank_sol_wallets_30d_tag_smart_degen_orderby_pnl_30d_direction_desc.json", "rank-wallets-sd-30d.json",
     "data.rank", 6),
    ("_vas_api_v1_wallet_activity_sol_wallet_DZAa55HwXgv5hStwaTEJGXZz1DhHejvpb7Yr762urXam_limit_20_type_buy_type_sell.json",
     "wallet-activity.json", "data.activities", 6),
    ("wallet_stat_sol_DZAa55HwXgv5hStwaTEJGXZz1DhHejvpb7Yr762urXam_30d", "wallet-stat-30d.json", None, 0),
    ("wallet_common_stat", "wallet-common-stat.json", None, 0),
    ("token_security_sol", "token-security.json", None, 0),
    ("token_dev_info", "token-dev-info.json", None, 0),
    ("api_v1_token_stat_sol", "token-stat.json", None, 0),
    ("token_holder_stat", "token-holder-stat.json", None, 0),
    ("token_launchpad_info", "token-launchpad-info.json", None, 0),
    ("dev_created_tokens", "dev-created-tokens.json", "data.tokens", 4),
    # saved names are cut at 120 characters; the renowned traders file ends in "_tag_re", the holders file
    # holds the tag=smart_degen answer (it was fetched last under the same cut name)
    ("orderby_profit_direction_desc_tag_re", "token-traders-renowned.json", "data.list", 2),
    ("orderby_amount_percentage_direction_", "token-holders-smart.json", "data.list", 3),
    ("POST_api_v1_mutil_window_token_info", "token-window-info.json", None, 0),
    ("rank_sol_swaps_1h_orderby_swaps_direction_desc_limit_20", "rank-swaps-1h.json", "data.rank", 3),
    ("new_pairs_1h", "new-pairs.json", "data.pairs", 3),
    ("POST_vas_api_v1_rank_sol", "trenches.json", None, 0),
]


def _trim(doc, path: str | None, keep: int):
    if not path:
        return doc
    node = doc
    parts = path.split(".")
    for p in parts[:-1]:
        node = node[p]
    if isinstance(node.get(parts[-1]), list):
        node[parts[-1]] = node[parts[-1]][:keep]
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="+")
    ap.add_argument("--dest", required=True)
    a = ap.parse_args()
    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    files = [f for d in a.src for f in sorted(Path(d).glob("*.json"))]
    for needle, name, path, keep in RULES:
        match = [f for f in files if needle in f.name]
        if not match:
            print(f"missing: {name} ({needle})")
            continue
        doc = json.loads(match[0].read_text())
        if name == "trenches.json":
            for k in ("new_creation", "pump", "completed"):
                doc["data"][k] = doc["data"][k][:2]
        doc = _trim(doc, path, keep)
        (dest / name).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        print(f"wrote {name} from {match[0].name}")


if __name__ == "__main__":
    main()
