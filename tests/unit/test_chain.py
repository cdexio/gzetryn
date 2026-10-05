"""Chain decoding on real getTransaction answers (version 1 transactions), compared with GMGN's events."""

from __future__ import annotations

import copy

import pytest

from gzetryn.trigger.chain import decode
from tests.conftest import load_fixture


@pytest.mark.parametrize("n", [0, 1])
def test_decode_matches_gmgn(n):
    case = load_fixture("chain-tx.json")[n]
    g, res = case["gmgn"], case["rpc"]["result"]
    assert res["version"] == 1
    trades = decode(res, g["wallet"], g["tx_hash"])
    assert len(trades) == 1
    t = trades[0]
    assert t.mint == g["mint"] and t.side == g["side"]
    assert t.token_amount == pytest.approx(g["token_amount"], rel=1e-6)
    # the chain delta is what the wallet really paid / received (DEX fees, tips): 0.4-2.2 % off GMGN's quote amount
    assert t.sol_amount == pytest.approx(g["sol_amount"], rel=0.03)
    assert t.trade_at is not None and t.tx_hash == g["tx_hash"]


def test_decode_rejects_failed_foreign_and_transfers():
    case = load_fixture("chain-tx.json")[0]
    g, res = case["gmgn"], case["rpc"]["result"]
    assert decode(res, "SomeOtherWallet1111111111111111111111111111", g["tx_hash"]) == []
    failed = copy.deepcopy(res)
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert decode(failed, g["wallet"], g["tx_hash"]) == []
    # a token change without the opposite SOL leg is a transfer, not a trade
    no_sol = copy.deepcopy(res)
    no_sol["meta"]["postBalances"] = list(no_sol["meta"]["preBalances"])
    no_sol["meta"]["fee"] = 0
    for b in no_sol["meta"].get("postTokenBalances") or []:
        if b.get("mint") == "So11111111111111111111111111111111111111112":
            b["uiTokenAmount"]["uiAmountString"] = next(
                (p["uiTokenAmount"]["uiAmountString"] for p in no_sol["meta"]["preTokenBalances"]
                 if p.get("accountIndex") == b.get("accountIndex")), b["uiTokenAmount"]["uiAmountString"])
    assert decode(no_sol, g["wallet"], g["tx_hash"]) == []
    assert decode(None, g["wallet"], g["tx_hash"]) == []
