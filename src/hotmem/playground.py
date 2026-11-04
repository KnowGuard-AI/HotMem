"""HotMem playground — interactive terminal UI for add/search/inspect.

Purpose:
    Make demos and debugging instant without curl or a Python script.
    Works against a running server (HTTP) or directly against a DB file.

Interface:
    run_playground(mode, db_path, url) -> None

Deps: rich (optional, [playground] extra), hotmem.client or hotmem.db

Extension: richer panels, pagination, or a full textual app live here.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hotmem.embed import Embedder
    from hotmem.rerank import Reranker

_RICH_TAG_RE = re.compile(r"\[/?[a-zA-Z0-9_ #=.]+\]")


def _strip_markup(text: str) -> str:
    return _RICH_TAG_RE.sub("", str(text))


class _PlainPanel:
    def __init__(self, renderable: Any, title: str | None = None, **kwargs: Any) -> None:
        self.renderable = renderable
        self.title = title
        self.kwargs = kwargs

    @classmethod
    def fit(cls, renderable: Any, title: str | None = None, **kwargs: Any) -> _PlainPanel:
        return cls(renderable, title=title, **kwargs)

    def render_lines(self) -> list[str]:
        header = f"=== {_strip_markup(self.title)} ===" if self.title else "=== Panel ==="
        lines = [header]
        if isinstance(self.renderable, _PlainTable):
            for tline in self.renderable.render_lines():
                lines.append(f"  {tline}")
        else:
            for line in _strip_markup(str(self.renderable)).splitlines():
                lines.append(f"  {line}")
        return lines


class _PlainTable:
    def __init__(self, title: str | None = None, **kwargs: Any) -> None:
        self.title = title
        self.kwargs = kwargs
        self.columns: list[str] = []
        self.rows: list[tuple[str, ...]] = []

    def add_column(self, name: str, **kwargs: Any) -> None:
        self.columns.append(_strip_markup(name))

    def add_row(self, *cells: Any) -> None:
        self.rows.append(tuple(_strip_markup(str(c)) for c in cells))

    def render_lines(self) -> list[str]:
        lines: list[str] = []
        if self.title:
            lines.append(f"--- {_strip_markup(self.title)} ---")
        if self.columns and self.kwargs.get("show_header", True):
            lines.append(" | ".join(self.columns))
        for row in self.rows:
            lines.append(" | ".join(row))
        return lines


class _PlainConsole:
    def print(self, obj: Any = "") -> None:
        if isinstance(obj, (_PlainPanel, _PlainTable)):
            for line in obj.render_lines():
                print(line)
        else:
            print(_strip_markup(str(obj)))

    def input(self, prompt: str = "") -> str:
        return input(_strip_markup(prompt))


def _import_rich():
    try:
        import sys

        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        console = Console() if sys.stdout.isatty() else Console(width=160)
        return console, Panel, Table
    except ImportError as err:
        raise ImportError(
            "Playground requires rich. Install with: uv pip install 'hotmem[playground]'"
        ) from err


class _DirectBackend:
    """Backend that operates directly against a SQLite DB file."""

    def __init__(
        self,
        db_path: str,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        from hotmem.db import MemoryDB

        self._db = MemoryDB(db_path)
        self._embedder = embedder
        self._reranker = reranker
        self.db_path = db_path

    def add(self, identifier: str, fact: str, **kwargs: Any) -> dict[str, Any]:
        import uuid

        from hotmem.embed import DEFAULT_EMBEDDER, pack_embedding
        from hotmem.swap import compute_content_hash

        namespace = str(kwargs.get("namespace") or "")
        content_hash = compute_content_hash(identifier, fact)
        existing = self._db.find_by_namespace_and_hash(namespace, content_hash)
        if existing is not None:
            return {
                "memory_id": str(existing["id"]),
                "content_hash": str(existing["content_hash"]),
                "deduplicated": True,
                "trace_ms": 0.0,
            }

        active = self._embedder if self._embedder is not None else DEFAULT_EMBEDDER
        memory_id = uuid.uuid4().hex
        vec = active.embed(fact)
        blob = pack_embedding(vec)
        self._db.insert(
            id=memory_id,
            identifier=identifier,
            fact_text=fact,
            embedding=blob,
            embedding_dim=active.descriptor.dimension,
            embedding_model=active.descriptor.key,
            source=kwargs.get("source", ""),
            importance=kwargs.get("importance", 0.5),
            content_hash=content_hash,
            namespace=namespace,
        )
        return {"memory_id": memory_id, "content_hash": content_hash, "trace_ms": 0.0}

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        from hotmem.search import search_memories

        return search_memories(
            self._db,
            query=query,
            top_k=top_k,
            embedder=self._embedder,
            reranker=self._reranker,
        )

    def count(self) -> int:
        return self._db.count()

    def close(self) -> None:
        self._db.close()


class _HttpBackend:
    """Backend that proxies to a running HotMem server with connection state tracking (#112)."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        session_id: str | None = None,
        harness_id: str | None = None,
        source: str | None = None,
        namespace: str | None = None,
        expected_store_id: str | None = None,
    ) -> None:
        from hotmem.client import HotMemClient

        self.url = url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.harness_id = harness_id
        self.source = source
        self.namespace = namespace
        self.expected_store_id = expected_store_id
        client_kwargs: dict[str, Any] = {}
        if token is not None:
            client_kwargs["token"] = token
        if session_id is not None:
            client_kwargs["session_id"] = session_id
        if harness_id is not None:
            client_kwargs["harness_id"] = harness_id
        if source is not None:
            client_kwargs["source"] = source
        if namespace is not None:
            client_kwargs["namespace"] = namespace
        if expected_store_id is not None:
            client_kwargs["expected_store_id"] = expected_store_id
        self._client = HotMemClient(self.url, **client_kwargs)
        self.connection_state: str = "connected"
        self.last_connected_at: str | None = None
        self.last_connected_monotonic: float | None = None
        self.last_error: str | None = None
        self.last_store_id: str | None = None
        self.last_generation: str | None = None
        self.generation_changed: bool = False

    def _record_success(self, data: dict[str, Any]) -> None:
        was_disconnected = self.connection_state == "disconnected"
        self.connection_state = "reconnected" if was_disconnected else "connected"
        self.last_error = None
        self.last_connected_at = str(data.get("observed_at") or datetime.now(UTC).isoformat())
        self.last_connected_monotonic = time.monotonic()
        new_gen = data.get("runtime_generation")
        if isinstance(new_gen, str) and new_gen:
            self.generation_changed = bool(
                self.last_generation is not None and self.last_generation != new_gen
            )
            self.last_generation = new_gen
        new_store = data.get("store_id")
        if isinstance(new_store, str) and new_store:
            self.last_store_id = new_store

    def _record_disconnect(self, err: Exception) -> dict[str, Any]:
        self.connection_state = "disconnected"
        self.last_error = f"{type(err).__name__}: {err}"
        stale_age_s = (
            round(time.monotonic() - self.last_connected_monotonic, 1)
            if self.last_connected_monotonic is not None
            else None
        )
        return {
            "status": "disconnected",
            "connection_state": "disconnected",
            "freshness_model": "poll_and_event_observed",
            "last_connected_at": self.last_connected_at,
            "stale_age_s": stale_age_s,
            "last_error": self.last_error,
            "store_id": self.last_store_id,
            "runtime_generation": self.last_generation,
        }

    def add(self, identifier: str, fact: str, **kwargs: Any) -> dict[str, Any]:
        call_kwargs: dict[str, Any] = {
            "identifier": identifier,
            "fact": fact,
            "source": kwargs.get("source", self.source or ""),
            "importance": kwargs.get("importance", 0.5),
        }
        if self.session_id is not None:
            call_kwargs["session_id"] = self.session_id
        if self.harness_id is not None:
            call_kwargs["harness_id"] = self.harness_id
        if self.namespace is not None:
            call_kwargs["namespace"] = self.namespace
        return self._client.add(**call_kwargs)

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        return self._client.search(query=query, top_k=top_k)

    def count(self) -> int:
        data = self._client.health()
        if isinstance(data, dict):
            self._record_success(data)
        return int(data["memory_count"])

    def status_snapshot(self) -> dict[str, Any]:
        import httpx

        try:
            data = dict(self._client.health())
        except (httpx.RequestError, httpx.HTTPStatusError, OSError) as err:
            if isinstance(err, httpx.HTTPStatusError) and err.response.status_code < 500:
                raise
            return self._record_disconnect(err)
        self._record_success(data)
        data["connection_state"] = self.connection_state
        data["generation_changed"] = self.generation_changed
        data["last_connected_at"] = self.last_connected_at
        return data

    def events(self, *, limit: int = 20, after_seq: int | None = None) -> dict[str, Any]:
        return self._client.events(limit=limit, after_seq=after_seq)

    def hygiene(self) -> dict[str, Any]:
        return self._client.hygiene()

    def revoke_policy(
        self,
        *,
        caller_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return self._client.revoke_policy(caller_id=caller_id, approval_id=approval_id)

    def reload_policy(self) -> dict[str, Any]:
        return self._client.reload_policy()

    def close(self) -> None:
        self._client.close()


def _render_management_status(console: Any, Panel: Any, Table: Any, snap: dict[str, Any]) -> None:
    conn = snap.get("connection_state", "connected")
    if conn == "disconnected":
        console.print(
            Panel.fit(
                f"Connection: DISCONNECTED\n"
                f"Freshness: {snap.get('freshness_model', 'poll_and_event_observed')}\n"
                f"Last connected: {snap.get('last_connected_at') or 'never'} "
                f"(stale_age_s={snap.get('stale_age_s')})\n"
                f"Last store_id: {snap.get('store_id') or '?'}\n"
                f"Last generation: {snap.get('runtime_generation') or '?'}\n"
                f"Error: {snap.get('last_error') or 'unreachable'}",
                title="Service & Freshness (Disconnected)",
                border_style="red",
            )
        )
        return

    emb = snap.get("embedding") or {}
    gen_note = " (generation_changed=True)" if snap.get("generation_changed") else ""
    service_lines = [
        f"Status: {snap.get('status', '?')} | Connection: {conn}{gen_note}",
        f"Freshness: {snap.get('freshness_model', 'poll_and_event_observed')} | "
        f"Observed at: {snap.get('observed_at', '?')} | Uptime: {snap.get('uptime_s', '?')}s",
        f"Generation: {snap.get('runtime_generation', '-')} | "
        f"Embedding: {emb.get('model', '?')} (dim={emb.get('dim', '?')})",
        f"Store ID: {snap.get('store_id', '-')} | Mount ID: {snap.get('mount_id', '-')} | "
        f"Store state: {snap.get('store_state', '-')} | "
        f"Visible memories: {snap.get('memory_count', '?')}",
    ]
    perms = snap.get("permission_state")
    if isinstance(perms, dict):
        is_mgmt = bool(perms.get("is_management"))
        if is_mgmt:
            service_lines.append(f"Total store memories: {snap.get('total_memory_count', 0)}")
        ns = ", ".join(perms.get("namespaces") or []) or "(none)"
        ops = ", ".join(perms.get("operations") or []) or "(none)"
        shares = perms.get("active_share_approvals") or []
        share_str = (
            ", ".join(
                f"{s.get('approval_id')}:{s.get('namespace')}"
                f"({','.join(s.get('identifiers') or [])})"
                for s in shares
                if isinstance(s, dict)
            )
            or "(none)"
        )
        service_lines.append(
            f"Caller: {perms.get('caller_id', '?')} | Management: {is_mgmt} | "
            f"Policy revision: {snap.get('policy_revision', '?')}"
        )
        service_lines.append(f"Namespaces: {ns} | Operations: {ops}")
        service_lines.append(f"Active share approvals: {share_str}")
        if is_mgmt:
            act_c = ", ".join(perms.get("active_caller_ids") or []) or "(none)"
            rev_c = ", ".join(perms.get("revoked_caller_ids") or []) or "(none)"
            rev_s = ", ".join(perms.get("revoked_share_approvals") or []) or "(none)"
            service_lines.append(
                f"Active callers: {act_c} | Revoked callers: {rev_c} | Revoked shares: {rev_s}"
            )
    console.print(Panel.fit("\n".join(service_lines), title="Service, Store & Permission State"))

    clients = snap.get("observed_clients")
    if isinstance(clients, list):
        ctbl = Table(title="Observed Clients (observation_basis=recorded_events)")
        ctbl.add_column("Caller")
        ctbl.add_column("Harness")
        ctbl.add_column("Session")
        ctbl.add_column("Source")
        ctbl.add_column("Namespace")
        ctbl.add_column("Last Seq")
        ctbl.add_column("Last Event")
        ctbl.add_column("Events")
        for c in clients:
            if not isinstance(c, dict):
                continue
            ctbl.add_row(
                str(c.get("caller_id") or "-"),
                str(c.get("harness_id") or "-"),
                str(c.get("session_id") or "-"),
                str(c.get("source") or "-"),
                str(c.get("namespace") or "-"),
                str(c.get("last_event_seq", "?")),
                str(c.get("last_event_type", "?")),
                str(c.get("observed_event_count", 0)),
            )
        console.print(ctbl)

    activity = snap.get("recent_activity")
    if isinstance(activity, dict):
        etbl = Table(
            title=(
                f"Permitted Activity (observation_basis="
                f"{activity.get('observation_basis', 'recorded_events')}, "
                f"latest_seq={activity.get('latest_seq')})"
            )
        )
        etbl.add_column("Seq")
        etbl.add_column("Type")
        etbl.add_column("Identifier")
        etbl.add_column("Namespace")
        etbl.add_column("Caller")
        etbl.add_column("Harness")
        etbl.add_column("Session")
        for ev in activity.get("events") or []:
            if not isinstance(ev, dict):
                continue
            etbl.add_row(
                str(ev.get("seq", "?")),
                str(ev.get("event_type", "?")),
                str(ev.get("identifier") or ev.get("memory_id") or "-"),
                str(ev.get("namespace") or "-"),
                str(ev.get("caller_id") or "-"),
                str(ev.get("harness_id") or "-"),
                str(ev.get("session_id") or "-"),
            )
        console.print(etbl)


def run_playground(
    *,
    db_path: str | None = None,
    url: str | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    token: str | None = None,
    session_id: str | None = None,
    harness_id: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    expected_store_id: str | None = None,
) -> None:
    """Run the interactive playground / detachable management loop.

    Exactly one of db_path or url must be provided. ``embedder`` (#78) and
    ``reranker`` (#80) apply to the direct backend only; ``None`` keeps
    the exact default ranking.
    """
    if db_path and url:
        raise ValueError("specify either db_path or url, not both")
    if not db_path and not url:
        raise ValueError("specify either --db or --url")

    try:
        console, Panel, Table = _import_rich()
    except ImportError:
        console, Panel, Table = _PlainConsole(), _PlainPanel, _PlainTable

    if url:
        backend: _DirectBackend | _HttpBackend = _HttpBackend(
            url,
            token=token,
            session_id=session_id,
            harness_id=harness_id,
            source=source,
            namespace=namespace,
            expected_store_id=expected_store_id,
        )
        where = f"server @ {url}"
    else:
        backend = _DirectBackend(  # type: ignore[arg-type]
            db_path, embedder=embedder, reranker=reranker
        )
        where = f"db @ {db_path}"

    console.print(
        Panel.fit(
            f"[bold]HotMem Playground[/bold]\n[dim]connected to {where}[/dim]\n"
            "[dim]commands: status | events | hygiene | revoke | reload | "
            "add | search | count | help | quit[/dim]",
            border_style="blue",
        )
    )

    try:
        import sys

        while True:
            try:
                sys.stdout.flush()
                line = console.input("[bold green]hotmem>[/bold green] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue

            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""

            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "help":
                console.print(
                    Panel.fit(
                        "[bold]status[/bold] / [bold]refresh[/bold]  "
                        "show service, store, client, activity & permissions\n"
                        "[bold]events[/bold] [limit]    "
                        "show permitted daemon events\n"
                        "[bold]hygiene[/bold]           "
                        "run authorized store hygiene inspection\n"
                        "[bold]revoke caller[/bold]     "
                        "revoke a caller credential (management)\n"
                        "[bold]revoke approval[/bold]   "
                        "revoke a share approval (management)\n"
                        "[bold]reload[/bold]            "
                        "reload daemon policy file (management)\n"
                        "[bold]add[/bold] <id> | <fact> add a memory\n"
                        "[bold]search[/bold] <query>    search memories\n"
                        "[bold]count[/bold]             show memory count\n"
                        "[bold]quit[/bold]              "
                        "exit (daemon continues running)",
                        title="Commands",
                        border_style="dim",
                    )
                )
            elif cmd in ("status", "refresh"):
                if isinstance(backend, _HttpBackend):
                    try:
                        snap = backend.status_snapshot()
                        _render_management_status(console, Panel, Table, snap)
                    except Exception as exc:
                        console.print(f"[red]Status error: {exc}[/red]")
                else:
                    console.print(
                        f"[cyan]{backend.count()}[/cyan] memories in local DB ({backend.db_path})"
                    )
            elif cmd == "events":
                if not isinstance(backend, _HttpBackend):
                    console.print("[red]events requires --url daemon mode[/red]")
                    continue
                limit = int(rest.strip()) if rest.strip().isdigit() else 20
                try:
                    ev_resp = backend.events(limit=limit)
                    etbl = Table(
                        title=(
                            f"Events (count={ev_resp.get('count', 0)}, "
                            f"next_seq={ev_resp.get('next_seq')})"
                        )
                    )
                    etbl.add_column("Seq")
                    etbl.add_column("Type")
                    etbl.add_column("Identifier")
                    etbl.add_column("Namespace")
                    etbl.add_column("Caller")
                    etbl.add_column("Harness")
                    etbl.add_column("Session")
                    for ev in ev_resp.get("events") or []:
                        payload = (ev.get("payload") or {}) if isinstance(ev, dict) else {}
                        prov_raw = payload.get("provenance_json") or payload.get("provenance")
                        prov = (
                            json.loads(prov_raw)
                            if isinstance(prov_raw, str)
                            else (prov_raw if isinstance(prov_raw, dict) else {})
                        )
                        etbl.add_row(
                            str(ev.get("seq", "?")),
                            str(ev.get("event_type", "?")),
                            str(payload.get("identifier") or ev.get("memory_id") or "-"),
                            str(ev.get("namespace") or payload.get("namespace") or "-"),
                            str(payload.get("caller_id") or prov.get("caller_id") or "-"),
                            str(payload.get("harness_id") or prov.get("harness_id") or "-"),
                            str(payload.get("session_id") or prov.get("session_id") or "-"),
                        )
                    console.print(etbl)
                except Exception as exc:
                    console.print(f"[red]Events error: {exc}[/red]")
            elif cmd == "hygiene":
                if not isinstance(backend, _HttpBackend):
                    console.print("[red]hygiene requires --url daemon mode[/red]")
                    continue
                try:
                    hyg = backend.hygiene()
                    stats = hyg.get("stats") or {}
                    warnings = hyg.get("warnings") or []
                    store_label = hyg.get("store_id") or backend.last_store_id or "-"
                    console.print(
                        f"hygiene: total_memories={stats.get('total_memories', 0)} "
                        f"warnings={len(warnings)} store_id={store_label}"
                    )
                except Exception as exc:
                    console.print(f"[red]Hygiene error: {exc}[/red]")
            elif cmd == "revoke":
                if not isinstance(backend, _HttpBackend):
                    console.print("[red]revoke requires --url daemon mode[/red]")
                    continue
                sub = rest.split(maxsplit=1)
                if len(sub) < 2 or sub[0].lower() not in ("caller", "approval"):
                    console.print("[red]Usage: revoke caller <id> | revoke approval <id>[/red]")
                    continue
                kind, target_id = sub[0].lower(), sub[1].strip()
                try:
                    if kind == "caller":
                        rev = backend.revoke_policy(caller_id=target_id)
                    else:
                        rev = backend.revoke_policy(approval_id=target_id)
                    console.print(
                        f"Revoked ({kind}={target_id}): "
                        f"revoked={rev.get('revoked')} policy_revision={rev.get('policy_revision')}"
                    )
                except Exception as exc:
                    console.print(f"[red]Revoke error: {exc}[/red]")
            elif cmd == "reload":
                if not isinstance(backend, _HttpBackend):
                    console.print("[red]reload requires --url daemon mode[/red]")
                    continue
                try:
                    rel = backend.reload_policy()
                    console.print(
                        f"Reloaded policy: policy_revision={rel.get('policy_revision')} "
                        f"credential_count={rel.get('credential_count')}"
                    )
                except Exception as exc:
                    console.print(f"[red]Reload error: {exc}[/red]")
            elif cmd == "count":
                try:
                    console.print(f"[cyan]{backend.count()}[/cyan] memories")
                except Exception as exc:
                    if isinstance(backend, _HttpBackend):
                        disc = backend._record_disconnect(exc)
                        console.print(f"[red]Disconnected: {disc.get('last_error')}[/red]")
                    else:
                        console.print(f"[red]Error: {exc}[/red]")
            elif cmd == "add":
                if "|" not in rest:
                    console.print("[red]Usage: add <identifier> | <fact>[/red]")
                    continue
                identifier, fact = rest.split("|", 1)
                try:
                    result = backend.add(identifier.strip(), fact.strip())
                except Exception as exc:
                    console.print(f"[red]Add error: {exc}[/red]")
                    continue
                table = Table(show_header=False, box=None)
                table.add_row("memory_id", result["memory_id"])
                table.add_row("content_hash", result.get("content_hash", ""))
                console.print(Panel(table, title="Added", border_style="green"))
            elif cmd == "search":
                if not rest:
                    console.print("[red]Usage: search <query>[/red]")
                    continue
                try:
                    results = backend.search(rest)
                except Exception as exc:
                    console.print(f"[red]Search error: {exc}[/red]")
                    continue
                if not results:
                    console.print("[dim]No matches.[/dim]")
                    continue
                table = Table(title=f"Search: {rest}")
                table.add_column("#", style="dim", width=3)
                table.add_column("Score", justify="right", style="cyan")
                table.add_column("Identifier", style="yellow")
                table.add_column("Content")
                for i, m in enumerate(results, 1):
                    table.add_row(
                        str(i),
                        str(m.get("score", "")),
                        m.get("identifier", ""),
                        m.get("content", "")[:80],
                    )
                console.print(table)
            else:
                console.print(f"[red]Unknown command: {cmd}[/red]  [dim]type 'help'[/dim]")
    finally:
        backend.close()
        console.print("[dim]bye.[/dim]")
