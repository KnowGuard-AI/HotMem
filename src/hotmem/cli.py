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


def _daemon_client_options(cmd):
    """Shared daemon client context options (#109, #110, #111)."""
    cmd = click.option(
        "--store-id",
        "expected_store_id",
        default=None,
        envvar="HOTMEM_STORE_ID",
        help="Expected daemon store_id (fails closed on mismatch).",
    )(cmd)
    cmd = click.option(
        "--namespace",
        "daemon_namespace",
        default=None,
        envvar="HOTMEM_NAMESPACE",
        help="Target namespace scope for daemon operation.",
    )(cmd)
    cmd = click.option(
        "--source",
        "daemon_source",
        default=None,
        envvar="HOTMEM_SOURCE",
        help="Explicit source attribution for daemon operation.",
    )(cmd)
    cmd = click.option(
        "--session",
        "daemon_session_id",
        default=None,
        envvar="HOTMEM_SESSION_ID",
        help="Explicit session ID for daemon operation.",
    )(cmd)
    return click.option(
        "--token",
        "daemon_token",
        default=None,
        envvar="HOTMEM_TOKEN",
        help="Bearer token for authenticated daemon access.",
    )(cmd)


def _make_daemon_client(
    url: str,
    *,
    token: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    expected_store_id: str | None = None,
):
    """Construct HotMemClient, passing optional context args only when set."""
    from hotmem.client import HotMemClient

    kwargs: dict[str, str] = {}
    if token is not None:
        kwargs["token"] = token
    if session_id is not None:
        kwargs["session_id"] = session_id
    if source is not None:
        kwargs["source"] = source
    if namespace is not None:
        kwargs["namespace"] = namespace
    if expected_store_id is not None:
        kwargs["expected_store_id"] = expected_store_id
    if not kwargs:
        return HotMemClient(url)
    return HotMemClient(url, **kwargs)


def _raise_cli_http_error(err: Exception) -> None:
    import httpx

    if isinstance(err, httpx.HTTPStatusError):
        resp = err.response
        try:
            body = resp.json()
            if isinstance(body, dict):
                code = body.get("error") or f"http_{resp.status_code}"
                msg = body.get("message") or body.get("detail") or resp.text
                raise click.ClickException(f"{code}: {msg}") from err
        except click.ClickException:
            raise
        except Exception:
            pass
        raise click.ClickException(f"HTTP {resp.status_code}: {resp.text}") from err
    if isinstance(err, httpx.RequestError):
        raise click.ClickException(f"daemon_unreachable: {err}") from err
    raise click.ClickException(str(err)) from err


@main.command()
@click.option("--port", default=8711, type=int, help="Port to listen on.")
@click.option(
    "--mount",
    default=None,
    envvar="HOTMEM_MOUNT",
    type=click.Path(),
    help="Mount directory path.",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    envvar="HOTMEM_DB",
    type=click.Path(),
    help="Explicit database path.",
)
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option(
    "--policy",
    "policy_path",
    default=None,
    envvar="HOTMEM_POLICY",
    type=click.Path(exists=True),
    help="Path to daemon policy JSON file (#109/#110).",
)
@click.option(
    "--store-id",
    "store_id",
    default=None,
    envvar="HOTMEM_STORE_ID",
    help="Explicit store identifier.",
)
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
    policy_path: str | None,
    store_id: str | None,
    vector_backend: str,
    embedder_spec: str | None,
    embedder_model_path: str | None,
    reranker_spec: str | None,
    reranker_lambda: float,
    reranker_pool: int,
):
    """Start the HotMem daemon/sidecar server."""
    import uvicorn

    from hotmem.mount import StoreOwnershipError, bootstrap_mount, ensure_store_available
    from hotmem.policy import PolicyConfig
    from hotmem.server import create_app

    if mount and db_path:
        raise click.ClickException("pass either --mount or --db, not both")
    if not mount and not db_path:
        raise click.ClickException(
            "serve requires an explicit store: pass --mount DIR or --db PATH "
            "(silent temporary database fallback is disabled in daemon mode)"
        )

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    reranker = _resolve_reranker_or_fail(reranker_spec, reranker_lambda, reranker_pool)

    swap_path = None
    policy: PolicyConfig | None = None

    if mount:
        config = bootstrap_mount(mount)
        db_path = str(config.db_path)
        swap_path = str(config.swap_path)
        if policy_path is None and (config.mount_path / "policy.json").exists():
            policy_path = str(config.mount_path / "policy.json")

    assert db_path is not None
    try:
        ensure_store_available(db_path)
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err

    if policy_path is not None:
        policy = PolicyConfig.load(policy_path)

    app = create_app(
        db_path=db_path,
        swap_path=swap_path,
        port=port,
        vector_backend=vector_backend,
        embedder=embedder,
        reranker=reranker,
        policy=policy,
        store_id=store_id,
    )

    _trace.info(
        "serve",
        f"starting server on {host}:{port}",
        detail={"db": db_path, "mount": mount, "store_id": app.state.runtime.store_id},
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err


@main.command()
@click.option(
    "--url",
    default=None,
    envvar="HOTMEM_URL",
    help="Running daemon URL for thin stdio-to-daemon relay mode (#109).",
)
@click.option(
    "--mount",
    default=None,
    envvar="HOTMEM_MOUNT",
    type=click.Path(),
    help="Mount directory path (embedded mode).",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    envvar="HOTMEM_DB",
    type=click.Path(),
    help="Explicit database path (embedded mode).",
)
@_daemon_client_options
@_embedder_options
@_reranker_options
def mcp(
    url: str | None,
    mount: str | None,
    db_path: str | None,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
    embedder_spec: str | None,
    embedder_model_path: str | None,
    reranker_spec: str | None,
    reranker_lambda: float,
    reranker_pool: int,
):
    """Start the HotMem MCP server on stdio (--url for daemon relay, --mount/--db for embedded)."""
    import asyncio

    try:
        from hotmem.mcp_server import run as run_mcp_server
        from hotmem.mcp_server import run_relay as run_mcp_relay
    except ImportError as err:
        raise click.ClickException(
            "MCP support is not installed. Install it with: uv pip install 'hotmem[mcp]'"
        ) from err

    if url and (mount or db_path):
        raise click.ClickException(
            "pass either --url (daemon relay mode) or --mount/--db (embedded mode), not both"
        )
    if not url and not mount and not db_path:
        raise click.ClickException(
            "mcp requires an explicit target: pass --url URL for daemon relay mode "
            "or --mount DIR / --db PATH for embedded mode"
        )

    if url:
        _trace.info(
            "mcp",
            "starting mcp stdio-to-daemon relay",
            detail={"url": url, "expected_store_id": expected_store_id},
        )
        asyncio.run(
            run_mcp_relay(
                base_url=url,
                token=daemon_token,
                session_id=daemon_session_id,
                source=daemon_source,
                namespace=daemon_namespace,
                expected_store_id=expected_store_id,
            )
        )
        return

    from hotmem.mount import StoreOwnershipError, bootstrap_mount, ensure_store_available

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    reranker = _resolve_reranker_or_fail(reranker_spec, reranker_lambda, reranker_pool)

    swap_path = None

    if mount:
        config = bootstrap_mount(mount)
        db_path = str(config.db_path)
        swap_path = str(config.swap_path)

    assert db_path is not None
    try:
        ensure_store_available(db_path)
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err

    _trace.info(
        "mcp",
        "starting mcp server on stdio",
        detail={"db": db_path, "mount": mount},
    )
    try:
        asyncio.run(
            run_mcp_server(
                db_path=db_path, swap_path=swap_path, embedder=embedder, reranker=reranker
            )
        )
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err


@main.command()
@click.option(
    "--file",
    "swap_file",
    default="swap.jsonl",
    type=click.Path(),
    help="Snapshot path: a directory (v2) or .jsonl/.jsonl.gz file (legacy).",
)
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database path.")
@click.option("--url", default=None, help="Running daemon URL (e.g. http://127.0.0.1:8711).")
@_daemon_client_options
@_embedder_options
def hydrate(
    swap_file: str,
    db_path: str | None,
    url: str | None,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
    embedder_spec: str | None,
    embedder_model_path: str | None,
):
    """Load a snapshot into the database or running daemon (v2 directory or legacy JSONL).

    Embeddings are rebuilt under the configured embedder (issues #78/#79):
    match the runtime the store is served with so imported rows are
    cosinely searchable — compatible stored vectors are reused either way.
    """
    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")
    if url is None and db_path is None:
        raise click.ClickException("hydrate requires --db PATH or --url URL")

    if url is not None:
        try:
            with _make_daemon_client(
                url,
                token=daemon_token,
                session_id=daemon_session_id,
                source=daemon_source,
                namespace=daemon_namespace,
                expected_store_id=expected_store_id,
            ) as client:
                res = client.hydrate(swap_file)
        except Exception as err:
            _raise_cli_http_error(err)
        get_renderer().summary(
            "hydrate",
            loaded=res["loaded"],
            skipped_dupes=res["skipped_dupes"],
            invalid=res.get("invalid", 0),
            embedding_reused=res.get("embedding_reused", 0),
            embedding_rebuilt=res.get("embedding_rebuilt", 0),
            embedding_missing=res.get("embedding_missing", 0),
            embedding_failed=res.get("embedding_failed", 0),
            annotations_merged=res.get("annotations_merged", 0),
            annotation_conflicts=res.get("annotation_conflicts", 0),
        )
        return

    assert db_path is not None
    from hotmem.db import MemoryDB
    from hotmem.mount import StoreOwnershipError, ensure_store_available

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    try:
        ensure_store_available(db_path)
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err

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
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database path.")
@click.option("--url", default=None, help="Running daemon URL (e.g. http://127.0.0.1:8711).")
@_daemon_client_options
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
def snapshot(
    swap_file: str,
    db_path: str | None,
    url: str | None,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
    copy_attachments: bool,
    package: bool,
    gz: bool,
):
    """Export database memories to a snapshot (v2 directory or legacy JSONL)."""
    from pathlib import Path

    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")
    if url is None and db_path is None:
        raise click.ClickException("snapshot requires --db PATH or --url URL")

    if url is not None:
        try:
            with _make_daemon_client(
                url,
                token=daemon_token,
                session_id=daemon_session_id,
                source=daemon_source,
                namespace=daemon_namespace,
                expected_store_id=expected_store_id,
            ) as client:
                res = client.snapshot(
                    swap_file,
                    copy_attachments=copy_attachments if copy_attachments else None,
                    package=package if package else None,
                    gz=gz if gz else None,
                )
        except Exception as err:
            _raise_cli_http_error(err)
        get_renderer().summary("snapshot", exported=res["exported"], path=res["path"])
        return

    assert db_path is not None
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
@click.option("--url", default=None, help="Explicit daemon URL (overrides --host/--port).")
@_daemon_client_options
def status(
    port: int,
    host: str,
    url: str | None,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
):
    """Check if a HotMem server is running."""
    import httpx

    from hotmem.client import _build_context_headers

    ui = get_renderer()
    base = url.rstrip("/") if url else f"http://{host}:{port}"
    health_url = f"{base}/v1/health"
    headers = _build_context_headers(
        token=daemon_token,
        session_id=daemon_session_id,
        source=daemon_source,
        namespace=daemon_namespace,
        expected_store_id=expected_store_id,
    )
    try:
        if headers:
            resp = httpx.get(health_url, headers=headers, timeout=3.0)
        else:
            resp = httpx.get(health_url, timeout=3.0)
        status_code = getattr(resp, "status_code", 200)
        if isinstance(status_code, int) and status_code >= 400:
            try:
                err_body = resp.json()
                code = err_body.get("error", f"http_{status_code}")
                msg = err_body.get("message") or err_body.get("detail") or ""
                click.echo(f"HotMem status failed ({code}): {msg}", err=True)
            except Exception:
                click.echo(f"HotMem status failed (HTTP {status_code})", err=True)
            raise SystemExit(1)
        data = resp.json()
        ui.status(data)
    except httpx.ConnectError as err:
        click.echo(f"No HotMem server found at {health_url}", err=True)
        raise SystemExit(1) from err


@main.command()
@click.argument("identifier")
@click.argument("fact")
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database file path.")
@click.option("--url", default=None, help="Running daemon URL (e.g. http://127.0.0.1:8711).")
@click.option("--importance", default=0.5, type=float, help="Importance score (0.0..1.0).")
@click.option("--ttl", "ttl_seconds", default=None, type=int, help="Optional TTL in seconds.")
@click.option("--idempotency-key", "idempotency_key", default=None, help="Operation retry key.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@_daemon_client_options
@_embedder_options
def add(
    identifier: str,
    fact: str,
    db_path: str | None,
    url: str | None,
    importance: float,
    ttl_seconds: int | None,
    idempotency_key: str | None,
    as_json: bool,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
    embedder_spec: str | None,
    embedder_model_path: str | None,
):
    """Add an inline fact to a running daemon (--url) or offline database (--db)."""
    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")
    if url is None and db_path is None:
        raise click.ClickException("add requires --db PATH or --url URL")

    if url is not None:
        try:
            with _make_daemon_client(
                url,
                token=daemon_token,
                session_id=daemon_session_id,
                source=daemon_source,
                namespace=daemon_namespace,
                expected_store_id=expected_store_id,
            ) as client:
                res = client.add(
                    identifier,
                    fact,
                    source=daemon_source or "",
                    importance=importance,
                    ttl_seconds=ttl_seconds,
                    idempotency_key=idempotency_key,
                )
        except Exception as err:
            _raise_cli_http_error(err)
        if as_json:
            click.echo(json.dumps(res, indent=2, default=str))
        else:
            get_renderer().summary(
                "add",
                memory_id=res["memory_id"],
                content_hash=res["content_hash"][:12],
            )
        return

    assert db_path is not None
    from hotmem.runtime import HotMemRuntime

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    rt = HotMemRuntime(db_path=db_path, embedder=embedder).open()
    try:
        ctx = rt.resolve_caller(
            session_id=daemon_session_id,
            source=daemon_source,
            namespace=daemon_namespace,
            idempotency_key=idempotency_key,
        )
        res = rt.add(
            ctx,
            identifier=identifier,
            fact=fact,
            source=daemon_source or "",
            importance=importance,
            ttl_seconds=ttl_seconds,
        )
    finally:
        rt.close()
    if as_json:
        click.echo(json.dumps(res, indent=2, default=str))
    else:
        get_renderer().summary(
            "add",
            memory_id=res["memory_id"],
            content_hash=res["content_hash"][:12],
        )


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
@_daemon_client_options
def search(
    query: str,
    db_path: str | None,
    url: str | None,
    top_k: int,
    as_json: bool,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
):
    """Search memories and print formatted results with scores."""
    rows = _run_search(
        query,
        db_path=db_path,
        url=url,
        top_k=top_k,
        token=daemon_token,
        session_id=daemon_session_id,
        source=daemon_source,
        namespace=daemon_namespace,
        expected_store_id=expected_store_id,
    )

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
    token: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    expected_store_id: str | None = None,
) -> list[dict]:
    """Resolve backend (HTTP server or local DB) and return search rows."""
    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")

    if url is not None:
        try:
            client = _make_daemon_client(
                url,
                token=token,
                session_id=session_id,
                source=source,
                namespace=namespace,
                expected_store_id=expected_store_id,
            )
            return client.search(query, top_k=top_k)
        except Exception as err:
            _raise_cli_http_error(err)

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
            with (
                open(swap_path, "w", encoding="utf-8", newline="\n") as f,
                ui.progress(total=None, desc="Reading source"),
            ):
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
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database path.")
@click.option("--url", default=None, help="Running daemon URL (e.g. http://127.0.0.1:8711).")
@click.option("--memory-id", "memory_id", required=True, help="Memory ID to promote.")
@click.option("--to", "to_state", required=True, help="Target state: HOT|READY|PROMOTED|ARCHIVED.")
@click.option("--reason", default=None, help="Optional reason for the transition.")
@click.option("--actor", default=None, help="Optional actor performing the transition.")
@_daemon_client_options
def promote(
    db_path: str | None,
    url: str | None,
    memory_id: str,
    to_state: str,
    reason: str | None,
    actor: str | None,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
):
    """Apply one promotion lifecycle transition (HOT→READY→PROMOTED→ARCHIVED)."""
    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")
    if url is None and db_path is None:
        raise click.ClickException("promote requires --db PATH or --url URL")

    if url is not None:
        try:
            with _make_daemon_client(
                url,
                token=daemon_token,
                session_id=daemon_session_id,
                source=daemon_source,
                namespace=daemon_namespace,
                expected_store_id=expected_store_id,
            ) as client:
                result = client.promote(memory_id, to_state, reason=reason, actor=actor)
        except Exception as err:
            _raise_cli_http_error(err)
        click.echo(
            f"{result['memory_id']}: {result['promotion_state']} (updated {result['updated_at']})"
        )
        return

    assert db_path is not None
    from hotmem.db import MemoryDB
    from hotmem.lifecycle import InvalidTransitionError, transition
    from hotmem.mount import StoreOwnershipError, ensure_store_available

    try:
        ensure_store_available(db_path)
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err

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
@click.option("--db", "db_path", default=None, type=click.Path(), help="Database path.")
@click.option("--url", default=None, help="Running daemon URL (e.g. http://127.0.0.1:8711).")
@click.option("--namespace", default=None, help="Filter by namespace.")
@click.option("--state", default=None, help="Filter by promotion state.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@_daemon_client_options
def candidates(
    db_path: str | None,
    url: str | None,
    namespace: str | None,
    state: str | None,
    as_json: bool,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
):
    """List memories flagged as promotion candidates."""
    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")
    if url is None and db_path is None:
        raise click.ClickException("candidates requires --db PATH or --url URL")

    if url is not None:
        try:
            with _make_daemon_client(
                url,
                token=daemon_token,
                session_id=daemon_session_id,
                source=daemon_source,
                namespace=daemon_namespace,
                expected_store_id=expected_store_id,
            ) as client:
                rows = client.candidates(namespace=namespace, state=state)
        except Exception as err:
            _raise_cli_http_error(err)
        if as_json:
            click.echo(json.dumps(rows, default=str, indent=2))
        else:
            for r in rows:
                mid = r.get("memory_id") or r.get("id")
                click.echo(f"{mid}\t{r['identifier']}\t{r.get('promotion_state', 'HOT')}")
        return

    assert db_path is not None
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
    from hotmem.mount import StoreOwnershipError, ensure_store_available

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    try:
        ensure_store_available(db_path)
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err
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
@click.option("--db", "db_path", default=None, type=click.Path(), help="Target database path.")
@click.option("--url", default=None, help="Running daemon URL (e.g. http://127.0.0.1:8711).")
@_daemon_client_options
@_embedder_options
def handoff_hydrate(
    package: str,
    db_path: str | None,
    url: str | None,
    daemon_token: str | None,
    daemon_session_id: str | None,
    daemon_source: str | None,
    daemon_namespace: str | None,
    expected_store_id: str | None,
    embedder_spec: str | None,
    embedder_model_path: str | None,
):
    """Hydrate a verified package into a target database or running daemon (atomic, idempotent)."""
    if url is not None and db_path is not None:
        raise click.ClickException("pass either --db or --url, not both")
    if url is None and db_path is None:
        raise click.ClickException("handoff hydrate requires --db PATH or --url URL")

    if url is not None:
        try:
            with _make_daemon_client(
                url,
                token=daemon_token,
                session_id=daemon_session_id,
                source=daemon_source,
                namespace=daemon_namespace,
                expected_store_id=expected_store_id,
            ) as client:
                res = client.handoff_hydrate(package)
        except Exception as err:
            _raise_cli_http_error(err)
        ui = get_renderer()
        ui.summary(
            "handoff-hydrate",
            handoff_id=res["handoff_id"],
            package_id=res["package_id"][:12],
            loaded=res["loaded"],
            skipped=res["skipped"],
            invalid=res.get("invalid", 0),
            already_applied=res.get("already_applied", False),
            brief=res.get("brief"),
            embedding_reused=res.get("embedding_reused", 0),
            embedding_rebuilt=res.get("embedding_rebuilt", 0),
            embedding_missing=res.get("embedding_missing", 0),
            embedding_failed=res.get("embedding_failed", 0),
            annotations_merged=res.get("annotations_merged", 0),
            annotation_conflicts=res.get("annotation_conflicts", 0),
        )
        return

    assert db_path is not None
    from hotmem.db import MemoryDB
    from hotmem.handoff.hydrate import hydrate_handoff as do_hydrate
    from hotmem.handoff.verify import HandoffError
    from hotmem.mount import StoreOwnershipError, ensure_store_available

    embedder = _resolve_embedder_or_fail(embedder_spec, embedder_model_path)
    try:
        ensure_store_available(db_path)
    except StoreOwnershipError as err:
        raise click.ClickException(str(err)) from err
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
