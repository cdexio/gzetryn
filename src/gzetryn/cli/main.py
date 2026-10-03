"""gzetryn command line.

Wallet management writes the database directly (the running service picks changes up within a minute).
`refresh`, `curate` and `stats` call the running service on 127.0.0.1, so GMGN traffic stays inside its budget.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import click

from gzetryn.config import PROJECT_DIR, get_settings
from gzetryn.log import setup_logging

ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
CLI_CONSUMER = "gzetryn-cli"


def _run(coro):
    return asyncio.run(coro)


def _address(value: str) -> str:
    if not ADDRESS_RE.fullmatch(value):
        raise click.BadParameter(f"not a Solana address: {value!r}")
    return value


def _store():
    from gzetryn.store.db import make_engine, make_sessionmaker
    from gzetryn.store.wallets import WalletStore

    s = get_settings()
    engine = make_engine(s.database_url, schema=s.db_schema)
    return engine, WalletStore(make_sessionmaker(engine))


def _api(method: str, path: str, timeout: float = 120.0) -> dict:
    s = get_settings()
    req = urllib.request.Request(
        f"http://{s.host}:{s.port}{path}", method=method, headers={"X-Consumer": CLI_CONSUMER}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise click.ClickException(f"HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise click.ClickException(f"service not reachable on {s.host}:{s.port} ({e.reason}); is gzetryn running?") from e


@click.group()
def cli() -> None:
    """gzetryn: GMGN web-data service for ZetrynAI."""


@cli.command()
def migrate() -> None:
    """Apply database migrations (alembic upgrade head)."""
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_DIR, check=True)


@cli.command()
def serve() -> None:
    """Run the API, directory, watcher and retention (127.0.0.1:8793)."""
    import uvicorn

    from gzetryn.api.main import build_app

    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_config=None, access_log=False)


@cli.command()
@click.option("--out", type=click.Path(path_type=Path), default=PROJECT_DIR / "docs" / "openapi.json")
def openapi(out: Path) -> None:
    """Write the OpenAPI schema (docs/openapi.json)."""
    from gzetryn.api.app import create_app

    app = create_app(lambda: None)  # schema only; the lifespan never runs
    out.write_text(json.dumps(app.openapi(), indent=1) + "\n")
    click.echo(f"wrote {out}")


# ---------- wallets ----------


@cli.group()
def wallets() -> None:
    """Directory: list curated/manual wallets, add/label/remove manual ones."""


@wallets.command("list")
@click.option(
    "--status",
    type=click.Choice(["curated", "manual", "active", "ranked", "inactive", "all"]),
    default="active",
    show_default=True,
)
@click.option("--tag", default=None)
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def wallets_list(status: str, tag: str | None, limit: int, as_json: bool) -> None:
    async def go():
        engine, store = _store()
        try:
            return await store.list_wallets(status, tag, None, limit)
        finally:
            await engine.dispose()

    rows = _run(go())
    if as_json:
        click.echo(json.dumps(rows, indent=1, default=str))
        return
    if not rows:
        click.echo("no wallets")
    for w in rows:
        m = w["metrics"]
        pr = "-" if m["realized_profit_30d"] is None else f"{m['realized_profit_30d']:,.0f}"
        wr = "-" if m["winrate_30d"] is None else f"{m['winrate_30d']:.2f}"
        tpd = "-" if m["trades_per_day_30d"] is None else f"{m['trades_per_day_30d']:.1f}"
        rank = str(w["curated_rank"] or "-")
        click.echo(
            f"{rank:>3} {w['status']:15} {w['address']} "
            f"{(w['label'] or w['name'] or '')[:24]:24} @{(w['twitter_username'] or '-')[:18]:18} "
            f"profit30d=${pr:>10} wr30d={wr:>4} trades/day={tpd:>5} last_trade={w['watch']['last_trade_at'] or '-'}"
        )


@wallets.command("add")
@click.argument("address", callback=lambda c, p, v: _address(v))
@click.option("--label", default=None)
@click.option("--tag", "tags", multiple=True, help="repeatable")
@click.option("--note", default=None)
def wallets_add(address: str, label: str | None, tags: tuple[str, ...], note: str | None) -> None:
    """Add (or update) a manual wallet. Curation never removes it."""
    from gzetryn.clock import Clock

    async def go():
        engine, store = _store()
        try:
            return await store.add_manual(address, label, list(tags) or None, note, CLI_CONSUMER, Clock().now())
        finally:
            await engine.dispose()

    w, created = _run(go())
    click.echo(f"{'added' if created else 'updated'} {w['address']} ({w['status']}); the service polls it within a minute")


@wallets.command("label")
@click.argument("address", callback=lambda c, p, v: _address(v))
@click.option("--label", default=None)
@click.option("--tag", "tags", multiple=True)
@click.option("--note", default=None)
def wallets_label(address: str, label: str | None, tags: tuple[str, ...], note: str | None) -> None:
    """Change label / tags / note of a manual wallet."""

    async def go():
        engine, store = _store()
        try:
            return await store.update_manual(address, label, list(tags) or None, note)
        finally:
            await engine.dispose()

    w = _run(go())
    if w is None:
        raise click.ClickException(f"{address} is not a manual wallet")
    click.echo(f"{address}: label={w['label']} tags={w['user_tags']}")


@wallets.command("remove")
@click.argument("address", callback=lambda c, p, v: _address(v))
def wallets_remove(address: str) -> None:
    """Clear the manual flag (data is kept; a curated wallet stays curated)."""

    async def go():
        engine, store = _store()
        try:
            return await store.remove_manual(address)
        finally:
            await engine.dispose()

    w = _run(go())
    if w is None:
        raise click.ClickException(f"{address} is not a manual wallet")
    click.echo(f"{address}: manual flag cleared (now {w['status']})")


# ---------- service actions ----------


@cli.command()
@click.option("--curate", is_flag=True, help="also run the curation now")
def refresh(curate: bool) -> None:
    """Run a rank refresh now (through the running service)."""
    click.echo(json.dumps(_api("POST", f"/v1/admin/refresh?curate={'true' if curate else 'false'}")["data"], indent=1))


@cli.command()
def curate() -> None:
    """Run the curation now from the latest rank snapshots (through the running service)."""
    click.echo(json.dumps(_api("POST", "/v1/admin/curate")["data"], indent=1))


@cli.command()
def stats() -> None:
    """Requests, errors, throttles, budget, job ages (from the running service)."""
    click.echo(json.dumps(_api("GET", "/v1/stats")["data"], indent=1))


@cli.command()
@click.argument("endpoint")
@click.argument("params", nargs=-1)
@click.option("--body", default=None, help="JSON body for POST endpoints")
def fetch(endpoint: str, params: tuple[str, ...], body: str | None) -> None:
    """One live GMGN call (dev aid), e.g. `gzetryn fetch rank_wallets period=7d tag=kol orderby=pnl_7d`.

    Path placeholders (period, address, mint, interval) are taken from the key=value list; the rest is the query.
    """
    from gzetryn.clock import Clock
    from gzetryn.config import get_settings as _gs
    from gzetryn.gateway.budget import Budget
    from gzetryn.gateway.gateway import Gateway
    from gzetryn.gmgn import endpoints as E
    from gzetryn.transport.http import HttpTransport

    if endpoint not in E.ALL:
        raise click.ClickException(f"endpoint must be one of {', '.join(E.ALL)}")
    ep = E.ALL[endpoint]
    kv = dict(p.split("=", 1) for p in params)
    path = {k: kv.pop(k) for k in ("period", "address", "mint", "interval") if k in kv}
    t = _gs().tunables

    async def go():
        tr = HttpTransport(t.transport.impersonate, t.transport.timeout_sec)
        gw = Gateway(t, tr, Budget(t.budget, Clock()))
        try:
            return await gw.call(ep, path=path, params=kv, body=json.loads(body) if body else None, priority="P0")
        finally:
            await tr.close()

    r = _run(go())
    click.echo(json.dumps(r.body, ensure_ascii=False, indent=1)[:20000])


if __name__ == "__main__":
    cli()
