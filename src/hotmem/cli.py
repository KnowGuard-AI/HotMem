"""HotMem CLI — command-line interface for the memory sidecar.

Purpose:
    Provide serve, hydrate, snapshot, and status commands.
    Entry point: `hotmem` (registered in pyproject.toml).

Interface:
    main() — Click group with subcommands

Deps: click, uvicorn, hotmem.server, hotmem.mount, hotmem.db, hotmem.swap, hotmem.trace
Extension: add new subcommands (e.g. `hotmem inspect`, `hotmem gc`) here.
"""

from __future__ import annotations

import tempfile

import click

from hotmem.trace import get_tracer

_trace = get_tracer("cli")


@click.group()
@click.version_option(package_name="hotmem")
def main():
    """HotMem — local-first memory sidecar for agent applications."""


@main.command()
@click.option("--port", default=8711, type=int, help="Port to listen on.")
@click.option("--mount", default=None, type=click.Path(), help="Mount directory path.")
@click.option("--db", "db_path", default=None, type=click.Path(), help="Explicit database path.")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
def serve(port: int, mount: str | None, db_path: str | None, host: str):
    """Start the HotMem sidecar server."""
    import uvicorn

    from hotmem.mount import bootstrap_mount
    from hotmem.server import create_app

    swap_path = None

    if mount:
        config = bootstrap_mount(mount)
        db_path = str(config.db_path)
        swap_path = str(config.swap_path)
    elif not db_path:
        db_path = tempfile.mktemp(suffix=".sqlite", prefix="hotmem_")
        _trace.warn(
            "serve",
            "no mount or db path specified, using temp db",
            detail={"path": db_path},
        )

    app = create_app(db_path=db_path, swap_path=swap_path, port=port)

    _trace.info(
        "serve",
        f"starting server on {host}:{port}",
        detail={"db": db_path, "mount": mount},
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


@main.command()
@click.option(
    "--file",
    "swap_file",
    default="swap.jsonl",
    type=click.Path(),
    help="Swap file path.",
)
@click.option("--db", "db_path", required=True, type=click.Path(), help="Database path.")
def hydrate(swap_file: str, db_path: str):
    """Load a swap file into the database."""
    from hotmem.db import MemoryDB
    from hotmem.swap import hydrate as do_hydrate

    db = MemoryDB(db_path)
    result = do_hydrate(db, swap_file)
    db.close()

    click.echo(f"Loaded: {result.loaded}, Skipped dupes: {result.skipped_dupes}")


@main.command()
@click.option(
    "--file",
    "swap_file",
    default="swap.jsonl",
    type=click.Path(),
    help="Output swap file path.",
)
@click.option("--db", "db_path", required=True, type=click.Path(), help="Database path.")
def snapshot(swap_file: str, db_path: str):
    """Export database memories to a swap file."""
    from hotmem.db import MemoryDB
    from hotmem.swap import snapshot as do_snapshot

    db = MemoryDB(db_path)
    result = do_snapshot(db, swap_file)
    db.close()

    click.echo(f"Exported: {result.exported} → {result.path}")


@main.command()
@click.option("--port", default=8711, type=int, help="Port to check.")
@click.option("--host", default="127.0.0.1", help="Host to check.")
def status(port: int, host: str):
    """Check if a HotMem server is running."""
    import httpx

    url = f"http://{host}:{port}/v1/health"
    try:
        resp = httpx.get(url, timeout=3.0)
        data = resp.json()
        click.echo(f"Status: {data['status']}")
        click.echo(f"Memories: {data['memory_count']}")
        click.echo(f"DB: {data['db_path']}")
        click.echo(f"Uptime: {data['uptime_s']}s")
    except httpx.ConnectError as err:
        click.echo(f"No HotMem server found at {url}", err=True)
        raise SystemExit(1) from err
