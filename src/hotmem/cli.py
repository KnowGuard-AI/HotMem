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


def _embedder_options(cmd):
    """Shared --embedder flags (issue #78): one explicit configuration path.

    Resolution happens before serving so an invalid selection fails fast;
    ``--embedder-model-path`` (or HOTMEM_EMBEDDER_MODEL_PATH) provisions the
    optional local semantic adapter — HotMem never downloads models.
    """
    cmd = click.option(
        "--embedder-model-path",
        "embedder_model_path",
        default=None,
        type=click.Path(),
        help="Local model artifact directory for --embedder local-semantic.",
    )(cmd)
    return click.option(
        "--embedder",
        "embedder_spec",
        default=None,
        type=click.Choice(["hash", "local-semantic"]),
        help=(
            "Embedding implementation (default: hash, the deterministic "
            "hotmem-hash-v1). 'local-semantic' requires the [semantic] extra "
            "and an explicitly provisioned local model."
        ),
    )(cmd)


def _resolve_embedder_or_fail(embedder_spec: str | None, embedder_model_path: str | None):
    """Resolve the runtime embedder, converting config errors to CLI errors."""
    from hotmem.embed import resolve_embedder_from_config

    try:
        return resolve_embedder_from_config(embedder_spec, model_path=embedder_model_path)
    except ValueError as err:
        raise click.ClickException(str(err)) from err


def _reranker_options(cmd):
    """Shared --reranker flags (issue #80): bounded second-stage selection.

    'none' (default) preserves the exact first-stage ranking; 'mmr' trades a
    fraction of relevance for diversity so near-duplicates stop consuming
    the top-k. Resolution happens before serving; the selection never
    enters canonical records or sync identity.
    """
    cmd = click.option(
        "--reranker-pool",
        "reranker_pool",
        default=50,
        type=click.IntRange(10, 200),
        help="MMR candidate pool size (10..200; default 50).",
    )(cmd)
    cmd = click.option(
        "--reranker-lambda",
        "reranker_lambda",
        default=0.5,
        type=click.FloatRange(0.0, 1.0),
        help="MMR relevance/diversity tradeoff in [0.0, 1.0] (default 0.5, "
        "the #80 evidence-driven setting).",
    )(cmd)
    return click.option(
        "--reranker",
        "reranker_spec",
        default=None,
        type=click.Choice(["none", "mmr"]),
        help=(
            "Optional bounded second-stage reranker (default: none — the exact "
            "current ranking). 'mmr' requires the #80 gate evidence; see "
            "bench/retrieval/post-p2-gate-80.md."
        ),
    )(cmd)


def _resolve_reranker_or_fail(
    reranker_spec: str | None, reranker_lambda: float, reranker_pool: int
):
    """Resolve the runtime reranker, converting config errors to CLI errors."""
    from hotmem.rerank import resolve_reranker_from_config

    try:
        return resolve_reranker_from_config(
            reranker_spec, lambda_=reranker_lambda, pool_limit=reranker_pool
        )
    except ValueError as err:
        raise click.ClickException(str(err)) from err


@main.command()
@click.option("--port", default=8711, type=int, help="Port to listen on.")
@click.option("--mount", default=None, type=click.Path(), help="Mount directory path.")
@click.option("--db", "db_path", default=None, type=click.Path(), help="Explicit database path.")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option(
    "--vector-index",
    "vector_backend",
    default="none",
    type=click.Choice(["none", "chroma"]),
    help="Optional derived vector index backend (default: none). The index is "
    "disposable and rebuildable; SQLite remains canonical storage.",
)
@_embedder_options
@_reranker_options
def serve(
    port: int,
    mount: str | None,
    db_path: str | None,
    host: str,
    vector_backend: str,
    embedder_spec: str | None,
    embedder_model_path: str | None,
    reranker_spec: str | None,
    reranker_lambda: float,
    reranker_pool: int,
):
    """Start the HotMem sidecar server."""
    import uvicorn

    from hotmem.mount import bootstrap_mount
    from hotmem.server import create_app

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    reranker = _resolve_reranker_or_fail(reranker_spec, reranker_lambda, reranker_pool)

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

    app = create_app(
        db_path=db_path,
        swap_path=swap_path,
        port=port,
        vector_backend=vector_backend,
        embedder=embedder,
        reranker=reranker,
    )

    _trace.info(
        "serve",
        f"starting server on {host}:{port}",
        detail={"db": db_path, "mount": mount},
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


@main.command()
@click.option("--mount", default=None, type=click.Path(), help="Mount directory path.")
@click.option("--db", "db_path", default=None, type=click.Path(), help="Explicit database path.")
@_embedder_options
@_reranker_options
def mcp(
    mount: str | None,
    db_path: str | None,
    embedder_spec: str | None,
    embedder_model_path: str | None,
    reranker_spec: str | None,
    reranker_lambda: float,
    reranker_pool: int,
):
    """Start the HotMem MCP server on stdio transport."""
    import asyncio

    try:
        from hotmem.mcp_server import run as run_mcp_server
    except ImportError as err:
        raise click.ClickException(
            "MCP support is not installed. Install it with: uv pip install 'hotmem[mcp]'"
        ) from err

    from hotmem.mount import bootstrap_mount

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    reranker = _resolve_reranker_or_fail(reranker_spec, reranker_lambda, reranker_pool)

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
    asyncio.run(
        run_mcp_server(db_path=db_path, swap_path=swap_path, embedder=embedder, reranker=reranker)
    )


@main.command()
@click.option(
    "--file",
    "swap_file",
    default="swap.jsonl",
    type=click.Path(),
    help="Snapshot path: a directory (v2) or .jsonl/.jsonl.gz file (legacy).",
)
@click.option("--db", "db_path", required=True, type=click.Path(), help="Database path.")
@_embedder_options
def hydrate(
    swap_file: str,
    db_path: str,
    embedder_spec: str | None,
    embedder_model_path: str | None,
):
    """Load a snapshot into the database (v2 directory or legacy JSONL).

    Embeddings are rebuilt under the configured embedder (issues #78/#79):
    match the runtime the store is served with so imported rows are
    cosinely searchable — compatible stored vectors are reused either way.
    """
    from hotmem.db import MemoryDB

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)

    # Route v2 directories through the snapshot dispatch (no progress bar);
    # legacy .jsonl/.jsonl.gz goes through swap.hydrate with the UI progress bar.
    is_dir_target = not swap_file.endswith((".jsonl", ".jsonl.gz"))
    if is_dir_target:
        from hotmem.interchange.hydrate import PackageError
        from hotmem.snapshot import hydrate as do_hydrate_v2
        from hotmem.snapshot.format import SnapshotChecksumError

        db = MemoryDB(db_path)
        try:
            result = do_hydrate_v2(db, swap_file, embedder=embedder)
        except (SnapshotChecksumError, PackageError) as err:
            db.close()
            reason = getattr(err, "reason", "checksum")
            raise click.ClickException(f"Snapshot verification failure ({reason}): {err}") from err
        db.close()
        get_renderer().summary(
            "hydrate",
            loaded=result.loaded,
            skipped_dupes=result.skipped_dupes,
            invalid=result.invalid,
            **result.disposition(),
        )
        return

    from hotmem.swap import hydrate as do_hydrate

    ui = get_renderer()
    is_gz = swap_file.lower().endswith(".gz")
    total = None if is_gz else (os.path.getsize(swap_file) if os.path.exists(swap_file) else 0)

    db = MemoryDB(db_path)
    with ui.progress(total=total, desc="Hydrating") as tick:
        result = do_hydrate(db, swap_file, on_progress=tick, embedder=embedder)
    db.close()

    ui.summary(
        "hydrate",
        loaded=result.loaded,
        skipped_dupes=result.skipped_dupes,
        invalid=result.invalid,
        **result.disposition(),
    )


@main.command()
@click.option(
    "--file",
    "swap_file",
    default="swap.jsonl",
    type=click.Path(),
    help="Snapshot path: a directory (v2/package) or .jsonl/.jsonl.gz file (legacy).",
)
@click.option("--db", "db_path", required=True, type=click.Path(), help="Database path.")
@click.option(
    "--attach",
    "copy_attachments",
    is_flag=True,
    default=False,
    help="Copy small file-backed byte ranges into attachments/ (v2 only).",
)
@click.option(
    "--package",
    "package",
    is_flag=True,
    default=False,
    help="Write a hotmem-interchange-v1 clone package (manifest + canonical payload) (#69).",
)
@click.option(
    "--gz",
    "gz",
    is_flag=True,
    default=False,
    help="Gzip the package payload (with --package): memories.jsonl.gz, byte-stable (mtime=0).",
)
def snapshot(swap_file: str, db_path: str, copy_attachments: bool, package: bool, gz: bool):
    """Export database memories to a snapshot (v2 directory or legacy JSONL).

    A path ending in .jsonl/.jsonl.gz writes a legacy single-file snapshot;
    any other path writes a v2 directory (manifest + memories.jsonl + optional
    attachments). Pass --attach to copy small file-backed byte ranges (<8 KB)
    into attachments/; large ranges stay referenced. Pass --package (optionally
    --gz) to write a hotmem-interchange-v1 clone package: versioned manifest,
    canonical record stream, atomic publish (#69).
    """
    from pathlib import Path

    from hotmem.db import MemoryDB

    # Route v2/package directories through the snapshot dispatch;
    # legacy .jsonl/.jsonl.gz goes through swap.snapshot with the UI progress bar.
    is_dir_target = package or not swap_file.endswith((".jsonl", ".jsonl.gz"))
    if is_dir_target:
        from hotmem.snapshot import snapshot as do_snapshot_v2

        db = MemoryDB(db_path)
        result = do_snapshot_v2(
            db,
            swap_file,
            package=package,
            gz=gz,
            copy_attachments=copy_attachments,
            base_dir=str(Path(db_path).resolve().parent),
        )
        db.close()
        get_renderer().summary("snapshot", exported=result.exported, path=result.path)
        return

    from hotmem.swap import snapshot as do_snapshot

    ui = get_renderer()
    db = MemoryDB(db_path)
    total = db.count()
    with ui.progress(total=total, desc="Snapshotting") as tick:
        result = do_snapshot(db, swap_file, on_progress=tick)
    db.close()

    ui.summary("snapshot", exported=result.exported, path=result.path)


@main.command()
@click.argument("path", type=click.Path(exists=True))
def verify(path: str):
    """Verify a snapshot directory or interchange package (#69).

    Checks required files, sizes, digests, and record counts before any
    restore; exits non-zero with structured diagnostics on failure.
    """
    from hotmem.interchange.hydrate import PackageError
    from hotmem.snapshot import verify as do_verify
    from hotmem.snapshot.format import SnapshotChecksumError

    ui = get_renderer()
    try:
        summary = do_verify(path)
    except (PackageError, SnapshotChecksumError) as err:
        detail = {"reason": getattr(err, "reason", None), "file": getattr(err, "file", None)}
        _trace.warn("verify", f"verification failed: {err}", detail=detail)
        ui.summary("verify", valid=False, reason=getattr(err, "reason", str(err)))
        raise click.ClickException(str(err)) from err
    ui.summary("verify", **summary)


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
@click.argument("uri")
@click.option(
    "--count-rows",
    is_flag=True,
    help=(
        "Compute row counts where cheap (CSV/JSONL). "
        "Parquet row count always comes from the footer."
    ),
)
@click.option(
    "--sample",
    "sample_size",
    default=5,
    type=int,
    help="Max sample rows to preview (CSV/JSONL).",
)
@click.option(
    "--full-validation",
    "full_validation",
    is_flag=True,
    help="JSONL: validate every line instead of only the sampled window. "
    "Inspection is advisory; full validation costs ~5x on large files.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON (bypasses the renderer, for scripting).",
)
def inspect(uri: str, count_rows: bool, sample_size: int, full_validation: bool, as_json: bool):
    """Inspect a local file's structure and provenance without ingesting it.

    Lightweight metadata-only inspection for CSV, JSONL, and Parquet files.
    Never copies file contents into the database — returns URI, size, checksum,
    columns, and an optional bounded sample. Unsupported formats and remote
    schemes fail with a clear error. Inspection is advisory: JSONL validation
    covers the sampled window unless --full-validation is passed, and the
    result declares its assurance level.
    """
    from hotmem.inspectors import UnsupportedFormatError, inspect_file
    from hotmem.storage import UnsupportedSchemeError

    try:
        inspection = inspect_file(
            uri,
            count_rows=count_rows,
            sample_size=sample_size,
            validation="full" if full_validation else "sampled",
        )
    except UnsupportedFormatError as err:
        raise click.ClickException(str(err)) from err
    except UnsupportedSchemeError as err:
        raise click.ClickException(str(err)) from err

    data = inspection.to_dict()

    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return

    ui = get_renderer()
    ui.summary(
        "inspect",
        format=data["format"],
        size=data["size"],
        rows=data["row_count"],
        checksum=str(data["checksum"])[:12] + "…",
    )
    click.echo(f"validation: {data['metadata'].get('validation', 'sampled')} (advisory)")
    if data["columns"]:
        click.echo(f"columns: {', '.join(data['columns'])}")
    if data["delimiter"]:
        click.echo(f"delimiter: {data['delimiter']!r}  has_header: {data['has_header']}")
    if data["num_row_groups"] is not None:
        types = ", ".join(data["schema_types"] or [])
        click.echo(f"row_groups: {data['num_row_groups']}  schema_types: {types}")
    if data["sample"]:
        click.echo("sample:")
        for row in data["sample"]:
            click.echo("  " + json.dumps(row, default=str))
    if data["unsupported_reason"]:
        click.echo(f"warning: {data['unsupported_reason']}", err=True)


@main.command()
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database file path.")
@click.option("--url", default=None, help="Running server URL (e.g. http://127.0.0.1:8711).")
@_embedder_options
@_reranker_options
def playground(
    db_path: str | None,
    url: str | None,
    embedder_spec: str | None,
    embedder_model_path: str | None,
    reranker_spec: str | None,
    reranker_lambda: float,
    reranker_pool: int,
):
    """Interactive terminal UI for add/search/inspect."""
    from hotmem.playground import run_playground

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    reranker = _resolve_reranker_or_fail(reranker_spec, reranker_lambda, reranker_pool)

    try:
        run_playground(db_path=db_path, url=url, embedder=embedder, reranker=reranker)
    except ImportError as err:
        raise click.ClickException(str(err)) from err
    except ValueError as err:
        raise click.ClickException(str(err)) from err


@main.command("import")
@click.option(
    "--from",
    "source",
    required=True,
    type=click.Choice(["mem0", "okf"], case_sensitive=False),
    help="Source memory system to import from (mem0) or an OKF v0.2 bundle (okf).",
)
@click.option(
    "--db",
    "source_db",
    required=True,
    type=click.Path(exists=True),
    help="Path to the source memory database (mem0) or OKF bundle directory (okf).",
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
@_embedder_options
def import_cmd(
    source: str,
    source_db: str,
    target_db: str | None,
    swap_out: str | None,
    embedder_spec: str | None,
    embedder_model_path: str | None,
):
    """Import memories from a foreign memory system into HotMem.

    One-command migration: read the source store, convert to HotMem swap JSONL,
    hydrate into the target DB. Embeddings are re-computed under the
    configured embedder (source dims differ, so reuse is not possible) —
    match the runtime the target is served with (issues #78/#79).

    OKF bundles (--from okf) convert every markdown concept page into one
    deterministic, reviewable JSONL record BEFORE hydration — keep it with
    --out to review exactly what will be loaded (#68).
    """
    import tempfile as _tempfile

    from hotmem.db import MemoryDB
    from hotmem.importers import IMPORTERS
    from hotmem.interchange.canonical import write_canonical
    from hotmem.swap import hydrate as do_hydrate
    from hotmem.swap import write_record

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    reader = IMPORTERS[source.lower()]
    # OKF records serialize canonically (sorted, compact, UTF-8) so the
    # reviewable JSONL is byte-stable across runs (#68 acceptance); mem0
    # keeps the historical swap-record serialization.
    serialize = write_canonical if source.lower() == "okf" else write_record

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
                    serialize(f, record)
        except (ValueError, FileNotFoundError) as err:
            raise click.ClickException(f"import from {source} failed: {err}") from err

        db = MemoryDB(target)
        try:
            total = os.path.getsize(swap_path) if os.path.exists(swap_path) else 0
            with ui.progress(total=total, desc="Hydrating") as tick:
                result = do_hydrate(db, swap_path, on_progress=tick, embedder=embedder)
        finally:
            db.close()

        ui.summary(
            "import",
            source=source,
            imported=result.loaded,
            skipped_dupes=result.skipped_dupes,
            invalid=result.invalid,
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


@main.command()
@click.option("--db", "db_path", required=True, type=click.Path(), help="Database path.")
@click.option("--memory-id", "memory_id", required=True, help="Memory ID to promote.")
@click.option("--to", "to_state", required=True, help="Target state: HOT|READY|PROMOTED|ARCHIVED.")
@click.option("--reason", default=None, help="Optional reason for the transition.")
@click.option("--actor", default=None, help="Optional actor performing the transition.")
def promote(db_path: str, memory_id: str, to_state: str, reason: str | None, actor: str | None):
    """Apply one promotion lifecycle transition (HOT→READY→PROMOTED→ARCHIVED)."""
    from hotmem.db import MemoryDB
    from hotmem.lifecycle import InvalidTransitionError, transition

    db = MemoryDB(db_path)
    try:
        result = transition(db, memory_id, to_state, reason=reason, actor=actor)
    except KeyError:
        click.echo(f"Memory not found: {memory_id}", err=True)
        raise SystemExit(1) from None
    except InvalidTransitionError as err:
        click.echo(str(err), err=True)
        raise SystemExit(1) from err
    except ValueError as err:
        click.echo(str(err), err=True)
        raise SystemExit(1) from err
    finally:
        db.close()
    click.echo(
        f"{result['memory_id']}: {result['promotion_state']} (updated {result['updated_at']})"
    )


@main.command()
@click.option("--db", "db_path", required=True, type=click.Path(), help="Database path.")
@click.option("--namespace", default=None, help="Filter by namespace.")
@click.option("--state", default=None, help="Filter by promotion state.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def candidates(db_path: str, namespace: str | None, state: str | None, as_json: bool):
    """List memories flagged as promotion candidates."""
    from hotmem.db import MemoryDB
    from hotmem.lifecycle import list_candidates

    db = MemoryDB(db_path)
    try:
        rows = list_candidates(db, namespace=namespace, state=state)
    finally:
        db.close()

    if as_json:
        click.echo(json.dumps(rows, default=str, indent=2))
    else:
        for r in rows:
            click.echo(f"{r['id']}\t{r['identifier']}\t{r.get('promotion_state', 'HOT')}")


@main.group()
def delta():
    """Verified one-way incremental sync between HotMem instances (#73)."""


@delta.command("produce")
@click.option(
    "--base",
    "base_pkg",
    required=True,
    type=click.Path(exists=True),
    help="Base package directory (hotmem-interchange-v1 clone).",
)
@click.option("--db", "db_path", required=True, type=click.Path(), help="Source database path.")
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(),
    help="Delta package output directory.",
)
@click.option(
    "--gz",
    "gz",
    is_flag=True,
    default=False,
    help="Gzip the operations payload (byte-stable, mtime=0).",
)
def delta_produce(base_pkg: str, db_path: str, out_dir: str, gz: bool):
    """Produce a verified delta package: base package -> current state.

    Deterministic compare-and-swap upserts sorted by record id; removals
    since the base are counted in the manifest and never applied (v1
    deletion policy). Re-run with no further changes yields an empty delta.
    """
    from hotmem.db import MemoryDB
    from hotmem.interchange.delta import produce_delta as do_produce
    from hotmem.interchange.hydrate import PackageError

    db = MemoryDB(db_path)
    try:
        result = do_produce(db, base_pkg, out_dir, gz=gz)
    except PackageError as err:
        db.close()
        raise click.ClickException(
            f"Base package verification failed ({err.reason}): {err}"
        ) from err
    db.close()
    ui = get_renderer()
    ui.summary(
        "delta-produce",
        added=result.added,
        changed=result.changed,
        removed_since_base=result.removed_since_base,
        total_ops=result.total_ops,
        path=result.path,
    )


@delta.command("apply")
@click.option(
    "--delta",
    "delta_dir",
    required=True,
    type=click.Path(exists=True),
    help="Delta package directory (hotmem-delta-v1).",
)
@click.option("--db", "db_path", required=True, type=click.Path(), help="Receiver database path.")
@_embedder_options
def delta_apply(
    delta_dir: str,
    db_path: str,
    embedder_spec: str | None,
    embedder_model_path: str | None,
):
    """Apply a verified delta to a receiver instance (all-or-nothing).

    Conflicts (diverged receiver, missing base) abort the whole delta and
    exit non-zero — the target is unchanged. Re-applying an applied delta
    loads zero changes.
    """
    from hotmem.db import MemoryDB
    from hotmem.interchange.delta import DeltaConflictError
    from hotmem.interchange.delta import apply_delta as do_apply
    from hotmem.interchange.hydrate import PackageError

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    db = MemoryDB(db_path)
    try:
        result = do_apply(db, delta_dir, embedder=embedder)
    except DeltaConflictError as err:
        db.close()
        for conflict in err.conflicts:
            click.echo(
                f"conflict {conflict.reason}: record={conflict.record_id} "
                f"op={conflict.op_id} expected={conflict.expected} actual={conflict.actual} "
                f"recovery={conflict.recovery}",
                err=True,
            )
        raise click.ClickException(f"Delta not applied — {len(err.conflicts)} conflict(s)") from err
    except PackageError as err:
        db.close()
        raise click.ClickException(f"Delta verification failed ({err.reason}): {err}") from err
    db.close()
    ui = get_renderer()
    ui.summary(
        "delta-apply",
        applied=result.applied,
        skipped=result.skipped,
        conflicts=len(result.conflicts),
        embedding_reused=result.embedding_reused,
        embedding_rebuilt=result.embedding_rebuilt,
        embedding_missing=result.embedding_missing,
        embedding_failed=result.embedding_failed,
    )


# ── handoff: Codex -> HotMem -> Claude session handoff (#101) ────────────────


@main.group()
def handoff():
    """Verified session handoff with preserved context (#101).

    Prepare a hotmem-handoff-v1 package from a documented source export,
    verify it fail-closed, inspect its coverage, and hydrate a target —
    atomic and idempotent, through the same core functions the MCP and
    HTTP surfaces use.
    """


@handoff.command("prepare")
@click.option(
    "--source",
    required=True,
    type=click.Path(exists=True),
    help="Source export directory (e.g. codex-export-v1).",
)
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(),
    help="Handoff package output directory.",
)
@click.option(
    "--mode",
    type=click.Choice(["resume", "archive"]),
    default="resume",
    help="resume: bounded brief + memories; archive: full ordered stream.",
)
@click.option(
    "--consent",
    required=True,
    help="Explicit consent statement. Required; capture never happens implicitly.",
)
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Require this session id in the export envelope (fail if it differs).",
)
def handoff_prepare(source: str, out_dir: str, mode: str, consent: str, session_id: str | None):
    """Prepare a verified handoff package from a source export."""
    from hotmem.handoff.codex_source import SourceError
    from hotmem.handoff.package import prepare_handoff as do_prepare

    try:
        result = do_prepare(source, out_dir, mode=mode, consent=consent, session_id=session_id)
    except (SourceError, ValueError) as err:
        raise click.ClickException(str(err)) from err

    ui = get_renderer()
    ui.summary(
        "handoff-prepare",
        handoff_id=result.handoff_id,
        package_id=result.package_id[:12],
        mode=mode,
        entries=result.manifest["counts"]["entries"],
        memories=result.manifest["counts"]["memories"],
        omissions=result.coverage["omitted_count"],
        redactions=result.coverage["redacted_count"],
        path=result.path,
        total_ms=result.timings_ms["total_ms"],
    )


@handoff.command("verify")
@click.argument("package", type=click.Path(exists=True))
def handoff_verify(package: str):
    """Fail-closed verification: exit non-zero on any integrity problem."""
    from hotmem.handoff.verify import HandoffError, verify_handoff

    try:
        verified = verify_handoff(package)
    except HandoffError as err:
        raise click.ClickException(
            f"verification failed ({err.reason})"
            + (f" [{err.file}]" if err.file else "")
            + (f": expected {err.expected}, got {err.actual}" if err.expected else "")
        ) from err

    ui = get_renderer()
    ui.summary(
        "handoff-verify",
        package_id=verified.manifest["package_id"][:12],
        mode=verified.manifest["mode"],
        entries=verified.entry_count,
        memories=verified.memory_count,
        valid=True,
    )


@handoff.command("inspect")
@click.argument("package", type=click.Path(exists=True))
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full report as JSON.",
)
def handoff_inspect(package: str, as_json: bool):
    """Read-only inspection: identity, coverage, omissions, failure reason."""
    from hotmem.handoff.verify import inspect_handoff

    report = inspect_handoff(package)
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True, default=str))
        return

    if not report["valid"]:
        failure = report["failure"]
        ui = get_renderer()
        ui.summary(
            "handoff-inspect",
            valid=False,
            reason=failure["reason"],
            file=failure["file"] or "-",
        )
        raise click.ClickException(f"package is not valid: {failure['reason']}")
    coverage = report["coverage"]
    counts = report["counts"]
    ui = get_renderer()
    ui.summary(
        "handoff-inspect",
        handoff_id=report["handoff_id"],
        package_id=report["package_id"][:12],
        mode=report["mode"],
        entries=counts["entries"],
        memories=counts["memories"],
        omissions=coverage["omitted_count"],
        redactions=coverage["redacted_count"],
        recoverable=coverage["recoverable_count"],
        source_adapter=report["source"]["adapter"],
        valid=True,
    )


@handoff.command("hydrate")
@click.argument("package", type=click.Path(exists=True))
@click.option("--db", "db_path", required=True, type=click.Path(), help="Target database path.")
@_embedder_options
def handoff_hydrate(
    package: str,
    db_path: str,
    embedder_spec: str | None,
    embedder_model_path: str | None,
):
    """Hydrate a verified package into a target database (atomic, idempotent)."""
    from hotmem.db import MemoryDB
    from hotmem.handoff.hydrate import hydrate_handoff as do_hydrate
    from hotmem.handoff.verify import HandoffError

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    db = MemoryDB(db_path)
    try:
        result = do_hydrate(db, package, embedder=embedder)
    except HandoffError as err:
        db.close()
        raise click.ClickException(
            f"hydration not applied — verification failed ({err.reason}): {err}"
        ) from err
    db.close()

    ui = get_renderer()
    ui.summary(
        "handoff-hydrate",
        handoff_id=result.handoff_id,
        package_id=result.package_id[:12],
        loaded=result.loaded,
        skipped=result.skipped_dupes,
        invalid=result.invalid,
        already_applied=result.already_applied,
        brief=result.brief_identifier,
        **result.disposition(),
    )
