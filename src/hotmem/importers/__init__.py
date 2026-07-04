"""HotMem importers — pluggable converters from foreign memory stores.

Purpose:
    Provide a registry of importers that read an external memory system's
    on-disk format and yield HotMem swap-record dicts (identifier, fact_text,
    created_at, source, ...). The CLI `hotmem import` command dispatches
    through this registry so new importers plug in without CLI changes.

Interface:
    IMPORTERS: dict[str, Callable[[Path], Iterator[dict]]]
    register(name, fn) -> None   # extension point for third-party importers

Deps: stdlib only at the package level; individual importer modules may
import their own (optional) deps lazily.

Extension: add a new module under importers/, implement a reader that yields
swap-record dicts, and register it here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from hotmem.importers.mem0 import read_mem0_sqlite

ImporterFn = Callable[[Path], Iterator[dict]]

IMPORTERS: dict[str, ImporterFn] = {
    "mem0": read_mem0_sqlite,
}


def register(name: str, fn: ImporterFn) -> None:
    """Register a third-party importer under `name`."""
    IMPORTERS[name] = fn
