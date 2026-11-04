"""HotMem UI — rendering seam for CLI output.

Purpose:
    Provide a single conditional surface for CLI output so that `rich` stays
    optional and the rest of the codebase never branches on renderer choice.

Interface:
    get_renderer() -> Renderer           # factory, picks Rich or Plain
    Renderer.status(data)                # health/summary panel
    Renderer.search_results(rows)        # formatted search hits with scores
    Renderer.progress(total, desc)        # context manager yielding tick(n)
    Renderer.summary(label, **kv)         # final one-line summary

Deps: click (always), rich (optional, lazy-imported at construction only)

Extension: add a JSON renderer or a batch/log renderer by extending the
factory.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import click

# Canonical /v1/health payload keys, in display order. Hoisted so both
# renderers iterate the same contract — adding a health field only edits here.
_STATUS_KEYS = ("status", "memory_count", "db_path", "uptime_s")


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def _use_rich() -> bool:
    """Decide whether the RichRenderer is appropriate for this process."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if not sys.stdout.isatty():
        return False
    return _rich_available()


def get_renderer() -> Renderer:
    """Return the best renderer for the current environment."""
    if _use_rich():
        return RichRenderer()
    return PlainRenderer()


class Renderer:
    """Protocol shared by Plain and Rich renderers."""

    def status(self, data: dict[str, Any]) -> None: ...

    def search_results(self, rows: list[dict[str, Any]]) -> None: ...

    @contextmanager
    def progress(self, total: int | None, desc: str = "") -> Iterator[Callable[[int], None]]: ...

    def summary(self, label: str, **kv: Any) -> None: ...


class PlainRenderer(Renderer):
    """Plain-text renderer — used when rich is absent or output is piped."""

    def status(self, data: dict[str, Any]) -> None:
        for key in _STATUS_KEYS:
            if key in data:
                click.echo(f"{key.replace('_', ' ').title()}: {data[key]}")
        if not (data.get("policy_enforced") or "store_id" in data):
            return

        conn_state = data.get("connection_state", "connected")
        freshness = data.get("freshness_model", "poll_and_event_observed")
        observed_at = data.get("observed_at", "?")
        click.echo(f"Connection: {conn_state} (freshness={freshness}, observed_at={observed_at})")
        click.echo(
            f"Store: {data.get('store_id', '?')} "
            f"(mount_id={data.get('mount_id', '?')}, state={data.get('store_state', '?')}, "
            f"generation={data.get('runtime_generation', '?')})"
        )
        perms = data.get("permission_state")
        if isinstance(perms, dict):
            is_mgmt = bool(perms.get("is_management"))
            if is_mgmt:
                click.echo(f"Total memory count: {data.get('total_memory_count', 0)}")
            ns_list = ", ".join(perms.get("namespaces") or []) or "(none)"
            op_list = ", ".join(perms.get("operations") or []) or "(none)"
            shares = perms.get("active_share_approvals") or []
            share_ids = (
                ", ".join(str(s.get("approval_id", "")) for s in shares if isinstance(s, dict))
                or "(none)"
            )
            click.echo(
                f"Permissions: caller={perms.get('caller_id', '?')} "
                f"management={is_mgmt} policy_revision={data.get('policy_revision', '?')} "
                f"namespaces=[{ns_list}] operations=[{op_list}] shares=[{share_ids}]"
            )
            if is_mgmt:
                active_callers = ", ".join(perms.get("active_caller_ids") or []) or "(none)"
                revoked_callers = ", ".join(perms.get("revoked_caller_ids") or []) or "(none)"
                revoked_shares = ", ".join(perms.get("revoked_share_approvals") or []) or "(none)"
                click.echo(
                    f"Policy state: active_callers=[{active_callers}] "
                    f"revoked_callers=[{revoked_callers}] revoked_shares=[{revoked_shares}]"
                )

        clients = data.get("observed_clients")
        if isinstance(clients, list):
            click.echo("Observed clients (observation_basis=recorded_events):")
            if not clients:
                click.echo("  (none observed in permitted scope)")
            for c in clients:
                if not isinstance(c, dict):
                    continue
                click.echo(
                    f"  - caller={c.get('caller_id') or '-'} "
                    f"harness={c.get('harness_id') or '-'} "
                    f"session={c.get('session_id') or '-'} "
                    f"source={c.get('source') or '-'} "
                    f"ns={c.get('namespace') or '-'} "
                    f"last_seq={c.get('last_event_seq', '?')} "
                    f"({c.get('last_event_type', '?')}) "
                    f"events={c.get('observed_event_count', 0)}"
                )

        activity = data.get("recent_activity")
        if isinstance(activity, dict):
            ev_list = activity.get("events") or []
            click.echo(
                "Permitted activity "
                f"(observation_basis={activity.get('observation_basis', 'recorded_events')}, "
                f"latest_seq={activity.get('latest_seq')}):"
            )
            if not ev_list:
                click.echo("  (no permitted events)")
            for ev in ev_list:
                if not isinstance(ev, dict):
                    continue
                click.echo(
                    f"  - seq={ev.get('seq')} {ev.get('event_type')} "
                    f"id={ev.get('identifier') or ev.get('memory_id') or '-'} "
                    f"ns={ev.get('namespace') or '-'} "
                    f"caller={ev.get('caller_id') or '-'} "
                    f"harness={ev.get('harness_id') or '-'} "
                    f"session={ev.get('session_id') or '-'}"
                )

    def search_results(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            click.echo("No memories found.")
            return
        for i, row in enumerate(rows, 1):
            score = row.get("score", "")
            ident = row.get("identifier", "")
            content = row.get("content", "")
            click.echo(f"{i}. [{score}] {ident}: {content}")

    @contextmanager
    def progress(self, total: int | None, desc: str = "") -> Iterator[Callable[[int], None]]:
        # Silent: no progress output when not a TTY / rich absent.
        def tick(_n: int = 0) -> None:
            pass

        yield tick

    def summary(self, label: str, **kv: Any) -> None:
        parts = [f"{k}={v}" for k, v in kv.items()]
        click.echo(f"{label}: " + ", ".join(parts)) if parts else click.echo(label)


class RichRenderer(Renderer):
    """Colored renderer backed by the optional `rich` dependency."""

    def __init__(self) -> None:
        from rich.console import Console

        self._console = Console()

    def status(self, data: dict[str, Any]) -> None:
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 1))
        table.add_column(style="cyan")
        table.add_column()
        for key in _STATUS_KEYS:
            if key in data:
                table.add_row(key.replace("_", " ").title(), str(data[key]))
        self._console.print(Panel(table, title="hotmem status", border_style="green"))

    def search_results(self, rows: list[dict[str, Any]]) -> None:
        from rich.table import Table

        if not rows:
            self._console.print("[dim]No memories found.[/dim]")
            return
        table = Table(title="Search results", show_lines=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("score", justify="right", style="yellow")
        table.add_column("identifier", style="cyan")
        table.add_column("content", overflow="fold")
        for i, row in enumerate(rows, 1):
            table.add_row(
                str(i),
                str(row.get("score", "")),
                str(row.get("identifier", "")),
                str(row.get("content", "")),
            )
        self._console.print(table)

    @contextmanager
    def progress(self, total: int | None, desc: str = "") -> Iterator[Callable[[int], None]]:
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        if total is None:
            # Indeterminate: spinner + description, no byte/ETA columns.
            prog = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                console=self._console,
            )
        else:
            prog = Progress(
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=self._console,
            )
        prog.start()
        try:
            task_id = prog.add_task(desc, total=total if total is not None else None)

            def tick(n: int = 1) -> None:
                prog.update(task_id, advance=n)

            yield tick
        finally:
            prog.stop()

    def summary(self, label: str, **kv: Any) -> None:
        parts = [f"[bold]{k}[/bold]=[green]{v}[/green]" for k, v in kv.items()]
        line = f"{label}: " + ", ".join(parts) if parts else label
        self._console.print(line)
