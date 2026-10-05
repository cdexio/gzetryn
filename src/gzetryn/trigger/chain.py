"""Decode a wallet's swap from a Solana transaction (spec §6.2): the feed's fallback while GMGN's `/vas/` group is
challenged.

Verified 2026-10-05 against 10 GMGN events: side and token amount match to rounding; the SOL amount differs by
0.4–2.2 % (the on-chain delta is what the wallet actually paid / received, DEX fees and tips included, which GMGN's
`quote_amount` leaves out; larger share on small trades). Transactions are version 0 or 1, so `getTransaction` must
allow `maxSupportedTransactionVersion: 1`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

WSOL = "So11111111111111111111111111111111111111112"


@dataclass
class ChainTrade:
    wallet: str
    tx_hash: str
    trade_at: datetime | None
    mint: str
    side: str  # buy | sell
    token_amount: float
    sol_amount: float  # wallet's SOL + wSOL change, the transaction fee removed (DEX fees/tips included)


def _keys(tx: dict) -> list[str]:
    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys") or []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in keys]


def decode(result: Any, wallet: str, signature: str) -> list[ChainTrade]:
    """`result` = getTransaction's result (jsonParsed). One ChainTrade per non-SOL mint whose balance owned by
    `wallet` changed. Failed transactions and transactions without such a change give []."""
    if not isinstance(result, dict):
        return []
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = _keys(result)
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol = (post[i] - pre[i]) / 1e9
            if i == 0:
                sol += (meta.get("fee") or 0) / 1e9  # the fee payer's network fee is not part of the trade
    deltas: dict[str, float] = {}
    for field, sign in (("preTokenBalances", -1.0), ("postTokenBalances", 1.0)):
        for b in meta.get(field) or []:
            if not isinstance(b, dict) or b.get("owner") != wallet:
                continue
            ui = (b.get("uiTokenAmount") or {}).get("uiAmountString")
            try:
                amount = float(ui) if ui not in (None, "") else 0.0
            except ValueError:
                continue
            deltas[b.get("mint")] = deltas.get(b.get("mint"), 0.0) + sign * amount
    sol += deltas.pop(WSOL, 0.0)
    bt = result.get("blockTime")
    at = datetime.fromtimestamp(bt, UTC) if isinstance(bt, (int, float)) and bt > 0 else None
    out = []
    for mint, d in deltas.items():
        if not mint or abs(d) <= 1e-12:
            continue
        side = "buy" if d > 0 else "sell"
        # a swap moves SOL the other way; a token change without it is a transfer/airdrop, not a trade
        if (side == "buy" and sol >= 0) or (side == "sell" and sol <= 0):
            continue
        out.append(ChainTrade(wallet, signature, at, mint, side, abs(d), abs(sol)))
    # multi-token swaps (token → token) have no SOL leg per mint; they are left to GMGN
    return out if len(out) == 1 else []
