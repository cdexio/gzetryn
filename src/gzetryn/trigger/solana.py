"""Small Solana helpers without extra dependencies: base58, program-derived addresses (PDA), pump.fun layouts.

PDA rule (Solana runtime): sha256(seeds ‖ [bump] ‖ program_id ‖ "ProgramDerivedAddress") for bump = 255…0, the first
result that is NOT a valid ed25519 point. The point check follows RFC 8032 §5.1.3 (decoding).
pump.fun layouts follow the public IDL (pump-fun/pump-public-docs, idl/pump.json, program
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P), verified by live decoding on 2026-10-05.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(B58)}

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_EVENT = hashlib.sha256(b"event:TradeEvent").digest()[:8]
COMPLETE_EVENT = hashlib.sha256(b"event:CompleteEvent").digest()[:8]
CREATE_EVENT = hashlib.sha256(b"event:CreateEvent").digest()[:8]
MIGRATION_EVENT = hashlib.sha256(b"event:CompletePumpAmmMigrationEvent").digest()[:8]
STANDARD_TOTAL_SUPPLY = 1_000_000_000_000_000  # Global.token_total_supply (1 B tokens, 6 decimals)
BONDING_CURVE_ACCOUNT = bytes([23, 183, 248, 55, 96, 216, 172, 96])  # IDL accounts.BondingCurve.discriminator
# Global.initial_real_token_reserves and initial_virtual - initial_real (verified on chain and in 2,180 + 6,558 live
# trades: virtual_token_reserves - real_token_reserves == 279.9 M tokens on every curve)
INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000
VIRTUAL_MINUS_REAL_TOKENS = 279_900_000_000_000
SOL_QUOTES = {"11111111111111111111111111111111", "So11111111111111111111111111111111111111112"}

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def on_curve(b: bytes) -> bool:
    """True when the 32 bytes decode to a valid ed25519 point (RFC 8032 §5.1.3)."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    sign = b[31] >> 7
    if y >= _P:
        return False
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P)) % _P
    vx2 = (v * x * x) % _P
    if vx2 == u:
        pass
    elif vx2 == (-u) % _P:
        x = (x * _SQRT_M1) % _P
    else:
        return False
    return not (x == 0 and sign == 1)


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    pid = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h), bump
    raise ValueError("no program address")


def bonding_curve_address(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)[0]


def progress(real_token_reserves: int) -> float:
    return max(0.0, min(1.0, 1.0 - real_token_reserves / INITIAL_REAL_TOKEN_RESERVES))


@dataclass
class Trade:
    """pump.fun TradeEvent (IDL order). Fields after `ix_name` exist only on newer program versions."""

    mint: str
    sol_amount: int
    token_amount: int
    is_buy: bool
    timestamp: int
    virtual_sol_reserves: int
    virtual_token_reserves: int
    real_sol_reserves: int
    real_token_reserves: int
    ix_name: str
    mayhem_mode: bool | None
    quote_mint: str | None
    creator: str | None = None

    @property
    def standard_curve(self) -> bool:
        """Standard Pump.fun token side: 793.1 M real tokens at the start (mayhem curves start with more)."""
        return (
            not self.mayhem_mode
            and self.virtual_token_reserves - self.real_token_reserves == VIRTUAL_MINUS_REAL_TOKENS
            and self.real_token_reserves <= INITIAL_REAL_TOKEN_RESERVES
        )

    @property
    def sol_quote(self) -> bool:
        return self.quote_mint is None or self.quote_mint in SOL_QUOTES

    @property
    def price_sol(self) -> float | None:
        """Price per whole token in SOL from the virtual reserves (6 token decimals, 9 lamport decimals)."""
        if not self.sol_quote or not self.virtual_token_reserves:
            return None
        return (self.virtual_sol_reserves / 1e9) / (self.virtual_token_reserves / 1e6)


def decode_trade(d: bytes) -> Trade:
    """Decode a TradeEvent payload (with its 8-byte discriminator). Raises struct.error on short data."""
    o = 8
    mint = b58encode(d[o : o + 32])
    o += 32
    sol, tok = struct.unpack_from("<QQ", d, o)
    o += 16
    is_buy = d[o] == 1
    o += 1 + 32  # is_buy, user
    (ts,) = struct.unpack_from("<q", d, o)
    o += 8
    vsol, vtok, rsol, rtok = struct.unpack_from("<QQQQ", d, o)
    o += 32
    o += 32 + 8 + 8  # fee_recipient, fee_basis_points, fee
    creator = b58encode(d[o : o + 32]) if o + 32 <= len(d) else None
    o += 32 + 8 + 8  # creator, creator_fee_basis_points, creator_fee
    o += 1 + 8 + 8 + 8 + 8  # track_volume, total_unclaimed, total_claimed, current_sol_volume, last_update_timestamp
    ix_name = ""
    mayhem = quote = None
    if o + 4 <= len(d):
        (n,) = struct.unpack_from("<I", d, o)
        o += 4
        ix_name = d[o : o + n].decode("utf-8", "replace")
        o += n
        if o < len(d):
            mayhem = d[o] == 1
            o += 1 + 8 + 8 + 8 + 8  # mayhem, cashback_fee_bps, cashback, buyback_fee_bps, buyback_fee
            if o + 4 <= len(d):
                (k,) = struct.unpack_from("<I", d, o)
                o += 4 + k * 40  # shareholders: Vec<{address: pubkey, bps: u64}>
                if o + 32 <= len(d):
                    quote = b58encode(d[o : o + 32])
    return Trade(mint, sol, tok, is_buy, ts, vsol, vtok, rsol, rtok, ix_name, mayhem, quote, creator)


_KINDS = {TRADE_EVENT: "trade", COMPLETE_EVENT: "complete", CREATE_EVENT: "create", MIGRATION_EVENT: "migration"}


def events_from_logs(logs: list[str] | None) -> list[tuple[str, bytes]]:
    """('trade' | 'complete' | 'create' | 'migration', payload) for pump.fun events in a transaction's log lines."""
    out = []
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        try:
            d = base64.b64decode(line[14:])
        except ValueError:
            continue
        kind = _KINDS.get(d[:8])
        if kind:
            out.append((kind, d))
    return out


def _string(d: bytes, o: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", d, o)
    return d[o + 4 : o + 4 + n].decode("utf-8", "replace"), o + 4 + n


@dataclass
class Created:
    mint: str
    name: str
    symbol: str
    creator: str
    timestamp: int
    token_total_supply: int | None
    mayhem_mode: bool | None


def decode_create(d: bytes) -> Created:
    """CreateEvent (IDL): name, symbol, uri (strings), mint, bonding_curve, user, creator, timestamp,
    virtual_token_reserves, virtual_sol_reserves, real_token_reserves, token_total_supply, token_program,
    is_mayhem_mode, …"""
    name, o = _string(d, 8)
    symbol, o = _string(d, o)
    _, o = _string(d, o)
    mint = b58encode(d[o : o + 32])
    o += 32 + 32 + 32  # mint, bonding_curve, user
    creator = b58encode(d[o : o + 32])
    o += 32
    (ts,) = struct.unpack_from("<q", d, o)
    o += 8
    supply = mayhem = None
    if o + 32 <= len(d):
        (supply,) = struct.unpack_from("<Q", d, o + 24)  # after virtual_token, virtual_sol, real_token
        o += 32 + 32  # reserves + supply, token_program
        if o < len(d):
            mayhem = d[o] == 1
    return Created(mint, name, symbol, creator, ts, supply, mayhem)


@dataclass
class Migration:
    mint: str
    bonding_curve: str
    timestamp: int
    pool: str
    quote_mint: str | None


def decode_migration(d: bytes) -> Migration:
    """CompletePumpAmmMigrationEvent (IDL): user, mint, mint_amount, sol_amount, pool_migration_fee, bonding_curve,
    timestamp, pool, quote_mint."""
    o = 8 + 32
    mint = b58encode(d[o : o + 32])
    o += 32 + 8 + 8 + 8
    curve = b58encode(d[o : o + 32])
    o += 32
    (ts,) = struct.unpack_from("<q", d, o)
    o += 8
    pool = b58encode(d[o : o + 32])
    o += 32
    quote = b58encode(d[o : o + 32]) if o + 32 <= len(d) else None
    return Migration(mint, curve, ts, pool, quote)


def decode_complete(d: bytes) -> tuple[str, str, int]:
    """CompleteEvent → (mint, bonding_curve, timestamp). IDL: user, mint, bonding_curve, timestamp, quote_mint."""
    (ts,) = struct.unpack_from("<q", d, 8 + 96)
    return b58encode(d[8 + 32 : 8 + 64]), b58encode(d[8 + 64 : 8 + 96]), ts


@dataclass
class BondingCurve:
    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int
    complete: bool
    creator: str
    is_mayhem_mode: bool | None


def decode_bonding_curve(data: bytes) -> BondingCurve | None:
    if data[:8] != BONDING_CURVE_ACCOUNT or len(data) < 8 + 41 + 32:
        return None
    vtok, vq, rtok, rq, supply = struct.unpack_from("<QQQQQ", data, 8)
    o = 8 + 40
    complete = data[o] == 1
    creator = b58encode(data[o + 1 : o + 33])
    mayhem = data[o + 33] == 1 if len(data) > o + 33 else None
    return BondingCurve(vtok, vq, rtok, rq, supply, complete, creator, mayhem)
