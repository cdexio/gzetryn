"""'Who to copy' research score (spec §8). Pure. Every component is returned next to the score.

score = w_profit × percentile(30d realized profit) + w_winrate × 30d win rate
      + w_positive_days × share of profitable days in daily_profit_7d + w_presence × presence
presence = share of the last N days in which the wallet appeared in a rank snapshot.
"""

from __future__ import annotations

from gzetryn.config import CopyScoreTunables


def positive_day_share(daily: list[dict] | None) -> float | None:
    vals = [d.get("profit") for d in daily or [] if d.get("profit") is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v > 0) / len(vals)


def percentiles(values: list[float]) -> list[float]:
    """Rank percentile in [0, 1] for each value (ties share the higher rank); one value → 1.0."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = sorted(values)
    out = []
    for v in values:
        below = sum(1 for x in order if x <= v) - 1
        out.append(below / (n - 1))
    return out


def score_rows(rows: list[dict], t: CopyScoreTunables) -> list[dict]:
    """rows: dicts with realized_profit_30d, winrate_30d, daily_profit_7d, presence_days. Returns rows + score, sorted."""
    profits = [r.get("realized_profit_30d") or 0.0 for r in rows]
    pct = percentiles(profits)
    out = []
    for r, p in zip(rows, pct, strict=True):
        wr = r.get("winrate_30d") or 0.0
        pos = positive_day_share(r.get("daily_profit_7d"))
        presence = min(1.0, (r.get("presence_days") or 0) / max(1, t.presence_days))
        score = t.w_profit * p + t.w_winrate * wr + t.w_positive_days * (pos or 0.0) + t.w_presence * presence
        out.append(
            {
                **r,
                "score": round(score, 4),
                "components": {
                    "profit_percentile": round(p, 4),
                    "winrate_30d": wr,
                    "positive_day_share_7d": pos,
                    "presence": round(presence, 4),
                },
            }
        )
    out.sort(key=lambda x: (-x["score"], x["address"]))
    return out
