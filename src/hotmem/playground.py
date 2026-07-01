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

from typing import Any


def _import_rich():
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        return Console(), Panel, Table
    except ImportError as err:
        raise ImportError(
            "Playground requires rich. Install with: uv pip install 'hotmem[playground]'"
        ) from err


class _DirectBackend:
    """Backend that operates directly against a SQLite DB file."""

    def __init__(self, db_path: str) -> None:
        from hotmem.db import MemoryDB

        self._db = MemoryDB(db_path)
        self.db_path = db_path

    def add(self, identifier: str, fact: str, **kwargs: Any) -> dict[str, Any]:
        import uuid

        from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
        from hotmem.swap import compute_content_hash

        memory_id = uuid.uuid4().hex
        content_hash = compute_content_hash(identifier, fact)
        vec = embed_text(fact)
        blob = pack_embedding(vec)
        self._db.insert(
            id=memory_id,
            identifier=identifier,
            fact_text=fact,
            embedding=blob,
            embedding_dim=EMBEDDING_DIM,
            embedding_model=EMBEDDING_MODEL,
            source=kwargs.get("source", ""),
            importance=kwargs.get("importance", 0.5),
            content_hash=content_hash,
        )
        return {"memory_id": memory_id, "content_hash": content_hash, "trace_ms": 0.0}

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        from hotmem.search import search_memories

        return search_memories(self._db, query=query, top_k=top_k)

    def count(self) -> int:
        return self._db.count()

    def close(self) -> None:
        self._db.close()


class _HttpBackend:
    """Backend that proxies to a running HotMem server."""

    def __init__(self, url: str) -> None:
        from hotmem.client import HotMemClient

        self._client = HotMemClient(url)
        self.url = url

    def add(self, identifier: str, fact: str, **kwargs: Any) -> dict[str, Any]:
        return self._client.add(
            identifier=identifier,
            fact=fact,
            source=kwargs.get("source", ""),
            importance=kwargs.get("importance", 0.5),
        )

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        return self._client.search(query=query, top_k=top_k)

    def count(self) -> int:
        return self._client.health()["memory_count"]

    def close(self) -> None:
        self._client.close()


def run_playground(*, db_path: str | None = None, url: str | None = None) -> None:
    """Run the interactive playground loop.

    Exactly one of db_path or url must be provided.
    """
    if db_path and url:
        raise ValueError("specify either db_path or url, not both")
    if not db_path and not url:
        raise ValueError("specify either --db or --url")

    console, Panel, Table = _import_rich()

    if url:
        backend: _DirectBackend | _HttpBackend = _HttpBackend(url)
        where = f"server @ {url}"
    else:
        backend = _DirectBackend(db_path)  # type: ignore[arg-type]
        where = f"db @ {db_path}"

    console.print(
        Panel.fit(
            f"[bold]HotMem Playground[/bold]\n[dim]connected to {where}[/dim]\n"
            "[dim]commands: add | search | count | help | quit[/dim]",
            border_style="blue",
        )
    )

    try:
        while True:
            try:
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
                        "[bold]add[/bold] <identifier> | <fact>   add a memory\n"
                        "[bold]search[/bold] <query>            search memories\n"
                        "[bold]count[/bold]                     show memory count\n"
                        "[bold]quit[/bold]                      exit",
                        title="Commands",
                        border_style="dim",
                    )
                )
            elif cmd == "count":
                console.print(f"[cyan]{backend.count()}[/cyan] memories")
            elif cmd == "add":
                if "|" not in rest:
                    console.print("[red]Usage: add <identifier> | <fact>[/red]")
                    continue
                identifier, fact = rest.split("|", 1)
                result = backend.add(identifier.strip(), fact.strip())
                table = Table(show_header=False, box=None)
                table.add_row("memory_id", result["memory_id"])
                table.add_row("content_hash", result.get("content_hash", ""))
                console.print(Panel(table, title="Added", border_style="green"))
            elif cmd == "search":
                if not rest:
                    console.print("[red]Usage: search <query>[/red]")
                    continue
                results = backend.search(rest)
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
