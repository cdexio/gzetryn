# Phase 14 — own smart/KOL buyer counts per token (replaces GMGN `/vas/` tags)

Owner decision D-2026-10-05-15 (2026-10-05, "bangun sendiri lah"): GMGN `/vas/` stays closed most
of each hour (40–44 min/h on 5 Oct, ladder at level 10). The two things only `/vas/` gave are the
per-token holder/trader counts by tag (`token_holder_stat`, `token_trader_stat`) and the list of
smart traders on a token (`token_traders`). The other `/vas/` data already has a replacement:
wallet activity comes from the chain decoder, the pump lists from the pump program stream, and the
anti-rug rates from `/api/v1/token_stat`, which is still open.

The goal is to count the smart/KOL wallets that bought a token from our own data: watch a wider set
of GMGN-tagged wallets on the chain, then count their buys per mint.

## Facts this plan rests on (measured 2026-10-05)

- GMGN `/defi/` rank lists are open: kol and smart_degen, 7d and 30d, 100 rows each. Latest snapshot:
  248 ranked wallets that are neither curated nor manual, all carrying a kol/smart_degen tag.
- 171 of the 248 trade ≤ 150 times a day (the curation bot filter); they average 46.2 trades/day,
  i.e. ≈ 7.9k trades/day, ≈ 5.5/min on average.
- The chain decoder paces getTransaction at 0.5 s on PublicNode plus 2 s on mainnet-beta, about 150
  calls/min together. The current 50 active wallets give 3.9k trades/day.
- **Public WS cap (probed 2026-10-05 ~17:25 UTC, 171 wallets on one connection):** the server closes the
  connection at the **100th subscription attempt** with code 1013, "Rate limit reached: Too many subscriptions
  attempted. Please open a new connection." This happened both at 20 ms spacing (98 accepted, closed after 3 s)
  and at 400 ms spacing (100 accepted, closed after 42 s), so it is a per-connection count, not a rate.
  Notifications: 7 in 75 s for 100 wallets.
- **Consequence for the design:**
  - Tagged wallets get their own connections of `per_connection` = 85 [TUNABLE], below the cap with room for churn.
  - Assignment is sticky, because every move between connections costs subscription attempts.
  - The main trigger connection (63 wallets + the pump program) also counts towards its own cap of 100. It
    reconnects on a close, and the reconnect resets the count.

## Units

1. **Tagged wallet set (WalletStore).**
   - Input: the latest rank snapshot. A wallet qualifies when it is ranked, neither curated nor manual,
     has a GMGN tag in `tagged.tags` [TUNABLE, default kol, smart_degen, renowned], and trades no more
     than `tagged.max_trades_per_day` [TUNABLE, default 150].
   - Order: by 30d realized profit. Cap: `tagged.max_wallets` [TUNABLE, default 200].
   - Output: the list of tagged wallets. Their status is "tagged". They never count as active, never
     reach the engine wallet sync, and never get a GMGN poll.

2. **Watcher.**
   - The trigger subscribes to active ∪ tagged.
   - A notified swap of a tagged wallet always goes to the chain decoder, whether or not `chain_first`
     is set, and is never routed to a GMGN wallet_activity poll or sweep.
   - `context_of` answers for tagged wallets with status "tagged", so decoded trades enter the feed
     table with `wallet_status = tagged`.
   - Health counts only active wallets.

3. **Feed API.**
   - `/v1/feed` leaves out `wallet_status = tagged` unless the caller passes `include_tagged=true`.
   - Engine copy trading (`kol_trades`, f1/f2, m3 KOL buys) therefore sees exactly what it saw before.

4. **Token part `tag_buyers`** (opt-in, computed from gzetryn's own feed table, no GMGN call).
   - Input: a mint and `window_min` [TUNABLE, default 30].
   - Output:
     - per tag (kol, smart_degen, renowned): distinct wallets that bought in the window;
     - how many of those still hold, i.e. bought more than they sold by token amount in the window;
     - the wallet list: address, tags, status, first buy, SOL in/out;
     - `universe`: the number of watched wallets per tag at that moment;
     - `source: "own"`.
   - It answers from the database only, so it stays available while `/vas/` is closed.

5. **Contract and docs.** `docs/contract.md`, `docs/openapi.json` and the spec are updated in the same
   commit.

## Engine side (web3-agent, plan `feature-own-tag-buyers`)

- The engine asks for `tag_buyers` in the same separate measure-only request as `snipers`, so m3
  never waits for it.
- It is merged into the survivor's `gmgn_token_intel` row as `parts.tag_buyers`. No strategy reads it.
- The anti-rug study can later compare it with the GMGN tag counts stored while `/vas/` was open.

## Verification

1. Public WS probe: subscribe the 171 tagged wallets on one connection. Record accepted vs rejected,
   notifications per minute and KB. If rejected > 0, the trigger shards subscriptions over a second
   connection (40 connections per IP are documented).
2. Tests on the VPS (nice):
   - the tagged-set rule;
   - the watcher routes tagged swaps to chain only;
   - the feed excludes tagged rows by default;
   - `tag_buyers` counts and the holding rule.
3. After deploy:
   - trigger subscriptions = active + tagged, 0 reconnects;
   - decoder backlog and lag p50/p90 against before;
   - tagged trades per hour;
   - `/v1/feed` count unchanged for the engine;
   - `tag_buyers` answered for the next Migration survivors.

## Risks

- **Decoder load.** About 3× the trades. Bursts can queue. The decoder already drops nothing and
  sweeps misses only for active wallets, so a tagged miss is simply lost [accepted: measure-only].
- **Coverage.** The universe is the GMGN top lists only (about 250 wallets), not GMGN's whole tag
  database. Counts read lower than GMGN's own; the study must compare like with like.
