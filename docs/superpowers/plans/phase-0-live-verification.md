# Phase 0 — live verification (GMGN web endpoints)

- Spec: `../specs/2026-10-03-gzetryn-design.md`, section 2.
- Result: `phase-0-report.md`.

## Goal

Know, from the VPS IP (`46.250.236.190`) and without login, which GMGN web
JSON endpoints gzetryn can use, their exact paths, parameters, answer
shapes, latency and throttling behaviour. Nothing is implemented on an
assumption: every endpoint the service calls is listed in the report with
the date it was verified.

## Method

1. **Discovery from GMGN's own web app.** Fetch the public HTML of
   `gmgn.ai/` and of a wallet, token and trade page; collect the Next.js
   bundle URLs; extract every string that looks like an API path
   (`/api/...`, `/defi/...`, `/vas/...`, `/pf/...`, `/mrwapi/...`,
   `/td/...`, `/tapi/...`). Read the code around the interesting paths to
   learn the parameter names, GET vs POST and the POST body shape.
2. **Live probes** with `tools/probe.py` on the VPS: curl_cffi
   `impersonate="chrome"`, header `Referer: https://gmgn.ai/`, no cookies,
   no login, one request every 1.2 s. Each answer: HTTP status, JSON or
   HTML, latency, size, envelope `code`/`msg`, and the shape of `data`.
   Bodies are saved for fixtures.
3. **Sample subjects** taken from live answers, not hard-coded guesses: an
   active KOL wallet from the KOL rank, a fresh pump.fun mint and its
   creator from the 1 h swaps rank.
4. **Freshness**: compare the newest `wallet_activity` timestamp with the
   wallet's `last_active` and with the wall clock.
5. **Rate test**: 60 requests at 1/s, then 90 at 2/s on the activity
   endpoint (the one the poller uses most); count non-200 answers and
   record latency percentiles.
6. **Fixtures**: `tools/make_fixtures.py` trims saved answers into
   `tests/fixtures/` (GMGN's real field names and types).

## Questions to answer

- Wallet: recent trades (paging, freshness), holdings, stats per period,
  profile/twitter.
- Token: info, price/pool, security (mint/freeze authority, top 10),
  dev/creator (address, holding/sold, previous tokens), launchpad and
  bonding progress, holder composition by tag, smart/KOL wallets that
  traded it.
- Market: trending, new pairs, pump.fun new / completing / completed.
- Which endpoints need login (to be avoided), error codes, Cloudflare
  behaviour, throttling.

## Done when

`phase-0-report.md` lists each endpoint as verified / needs login /
not found, with the facts above, and the spec is updated from it.
