"""HotMem CLI — command-line interface for the memory sidecar.

Purpose:
    Provide serve, mcp, hydrate, snapshot, and status commands.
    Entry point: `hotmem` (registered in pyproject.toml).

Interface:
    main() — Click group with subcommands

Deps: click, uvicorn, hotmem.server, hotmem.mcp_server, hotmem.mount, hotmem.db,
      hotmem.swap, hotmem.trace
Extension: add new subcommands (e.g. `hotmem inspect`, `hotmem gc`) here.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path as _Path

import click

from hotmem.trace import get_tracer
from hotmem.ui import get_renderer

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
@click.option("--mount", default=None, type=click.Path(), help="Mount directory path.")
@click.option("--db", "db_path", default=None, type=click.Path(), help="Explicit database path.")
def mcp(mount: str | None, db_path: str | None):
    """Start the HotMem MCP server on stdio transport."""
    import asyncio

    try:
        from hotmem.mcp_server import run as run_mcp_server
    except ImportError as err:
        raise click.ClickException(
            "MCP support is not installed. Install it with: uv pip install 'hotmem[mcp]'"
        ) from err

    from hotmem.mount import bootstrap_mount

    swap_path = None

    if mount:
        config = bootstrap_mount(mount)
        db_path = str(config.db_path)
        swap_path = str(config.swap_path)
    elif not db_path:
        db_path = tempfile.mktemp(suffix=".sqlite", prefix="hotmem_")
        _trace.warn(
            "mcp",
            "no mount or db path specified, using temp db",
            detail={"path": db_path},
        )

    _trace.info(
        "mcp",
        "starting mcp server on stdio",
        detail={"db": db_path, "mount": mount},
    )
    asyncio.run(run_mcp_server(db_path=db_path, swap_path=swap_path))


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

    ui = get_renderer()
    # For .jsonl.gz the on-disk (compressed) size != uncompressed bytes advanced
    # by on_progress, so the byte bar would overshoot; use indeterminate instead.
    is_gz = swap_file.lower().endswith(".gz")
    total = None if is_gz else (os.path.getsize(swap_file) if os.path.exists(swap_file) else 0)

    db = MemoryDB(db_path)
    with ui.progress(total=total, desc="Hydrating") as tick:
        result = do_hydrate(db, swap_file, on_progress=tick)
    db.close()

    ui.summary("hydrate", loaded=result.loaded, skipped_dupes=result.skipped_dupes)


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

    ui = get_renderer()
    db = MemoryDB(db_path)
    total = db.count()
    with ui.progress(total=total, desc="Snapshotting") as tick:
        result = do_snapshot(db, swap_file, on_progress=tick)
    db.close()

    ui.summary("snapshot", exported=result.exported, path=result.path)


@main.command()
@click.option("--port", default=8711, type=int, help="Port to check.")
@click.option("--host", default="127.0.0.1", help="Host to check.")
def status(port: int, host: str):
    """Check if a HotMem server is running."""
    import httpx

    ui = get_renderer()
    url = f"http://{host}:{port}/v1/health"
    try:
        resp = httpx.get(url, timeout=3.0)
        data = resp.json()
        ui.status(data)
    except httpx.ConnectError as err:
        click.echo(f"No HotMem server found at {url}", err=True)
        raise SystemExit(1) from err


@main.command()
@click.argument("query")
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database file path.")
@click.option("--url", default=None, help="Running server URL (e.g. http://127.0.0.1:8711).")
@click.option("--top-k", "top_k", default=5, type=int, help="Maximum results.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON (bypasses the renderer, for scripting).",
)
def search(query: str, db_path: str | None, url: str | None, top_k: int, as_json: bool):
    """Search memories and print formatted results with scores."""
    rows = _run_search(query, db_path=db_path, url=url, top_k=top_k)

    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    get_renderer().search_results(rows)


def _run_search(
    query: str,
    *,
    db_path: str | None,
    url: str | None,
    top_k: int,
) -> list[dict]:
    """Resolve backend (HTTP server or local DB) and return search rows."""
    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")

    if url is not None:
        from hotmem.client import HotMemClient

        return HotMemClient(url).search(query, top_k=top_k)

    from hotmem.db import MemoryDB
    from hotmem.search import search_memories

    if not db_path:
        raise click.ClickException("search requires --db PATH or --url URL")
    db = MemoryDB(db_path)
    try:
        return search_memories(db, query=query, top_k=top_k)
    finally:
        db.close()


@main.command()
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(),
    help="Output file path. If omitted, print to stdout.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "yaml"]),
    default="json",
    help="Output format.",
)
def openapi(output: str | None, fmt: str):
    """Export the OpenAPI specification."""
    from hotmem.openapi import dump_openapi, export_openapi

    if output:
        path = dump_openapi(output, fmt=fmt)
        click.echo(f"OpenAPI spec written to {path}")
    else:
        spec = export_openapi()
        if fmt == "yaml":
            try:
                import yaml
            except ImportError as err:
                raise click.ClickException(
                    "YAML output requires PyYAML. Use --format json instead."
                ) from err
            click.echo(yaml.dump(spec, sort_keys=False, default_flow_style=False))
        else:
            click.echo(json.dumps(spec, indent=2))


@main.command()
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database file path.")
@click.option("--url", default=None, help="Running server URL (e.g. http://127.0.0.1:8711).")
def playground(db_path: str | None, url: str | None):
    """Interactive terminal UI for add/search/inspect."""
    from hotmem.playground import run_playground

    try:
        run_playground(db_path=db_path, url=url)
    except ImportError as err:
        raise click.ClickException(str(err)) from err
    except ValueError as err:
        raise click.ClickException(str(err)) from err


@main.command("import")
@click.option(
    "--from",
    "source",
    required=True,
    type=click.Choice(["mem0"], case_sensitive=False),
    help="Source memory system to import from.",
)
@click.option(
    "--db",
    "source_db",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the source memory database (e.g. mem0's history SQLite DB).",
)
@click.option(
    "--target",
    "target_db",
    default=None,
    type=click.Path(),
    help="HotMem database to hydrate into. Defaults to a temp DB.",
)
@click.option(
    "--out",
    "swap_out",
    default=None,
    type=click.Path(),
    help="Keep the intermediate HotMem swap JSONL at this path (default: temp, deleted).",
)
def import_cmd(source: str, source_db: str, target_db: str | None, swap_out: str | None):
    """Import memories from a foreign memory system into HotMem.

    One-command migration: read the source store, convert to HotMem swap JSONL,
    hydrate into the target DB. Embeddings are re-computed by HotMem's
    embedder (source dims differ, so reuse is not possible).
    """
    import tempfile as _tempfile

    from hotmem.db import MemoryDB
    from hotmem.importers import IMPORTERS
    from hotmem.swap import hydrate as do_hydrate
    from hotmem.swap import write_record

    reader = IMPORTERS[source.lower()]

    ui = get_renderer()

    # Use a private temp dir for transient artifacts so both the swap JSONL
    # and the target DB are cleaned up atomically and never leave predictable
    # paths on disk. mkdtemp creates the dir atomically (no mktemp race).
    tmp_dir = _tempfile.mkdtemp(prefix="hotmem_import_")
    swap_keep = swap_out is not None
    target_keep = target_db is not None
    swap_path = swap_out or os.path.join(tmp_dir, "import.jsonl")
    target = target_db or os.path.join(tmp_dir, "hotmem.sqlite")

    try:
        try:
            # Reading phase: indeterminate progress (we don't know the row
            # count up front); the byte-total bar applies to the hydrate phase.
            with open(swap_path, "w") as f, ui.progress(total=None, desc="Reading source"):
                for record in reader(_Path(source_db)):
                    write_record(f, record)
        except (ValueError, FileNotFoundError) as err:
            raise click.ClickException(f"import from {source} failed: {err}") from err

        db = MemoryDB(target)
        try:
            total = os.path.getsize(swap_path) if os.path.exists(swap_path) else 0
            with ui.progress(total=total, desc="Hydrating") as tick:
                result = do_hydrate(db, swap_path, on_progress=tick)
        finally:
            db.close()

        ui.summary(
            "import",
            source=source,
            imported=result.loaded,
            skipped_dupes=result.skipped_dupes,
            target=target,
        )
    finally:
        # Clean up transient artifacts. Kept paths (--out / --target) survive.
        if not swap_keep and os.path.exists(swap_path):
            os.remove(swap_path)
        if not target_keep and os.path.exists(target):
            os.remove(target)
        # Remove the temp dir if empty (kept artifacts may live elsewhere).
        if os.path.isdir(tmp_dir) and not os.listdir(tmp_dir):
            os.rmdir(tmp_dir)
