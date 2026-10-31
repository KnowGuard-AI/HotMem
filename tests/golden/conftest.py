"""Compatibility golden tests for HotMem's file-native evolution (issue #54).

Purpose:
    Make the non-breaking public API contract executable. These tests lock down
    the *current* public behavior of the API,
    swap files, Python client, and MCP server so that file/bundle/snapshot
    work landing in #38-#43 cannot silently drift the existing surface.

Design:
    - Volatile values (uuids, hashes, timestamps, floats, db paths) are masked
      to typed sentinels so snapshots stay deterministic across runs while
      their *types* remain locked.
    - Golden expected shapes live under ``fixtures/`` as JSON and are loaded
      once per session.
    - Tests assert exact key sets and masked shapes, not free-form equality,
      so a new optional field added by a future ticket fails *here* first and
      forces an intentional update to the golden contract.

Extension:
    When a vNext ticket intentionally extends a public shape, update the
    corresponding fixture in the same PR and add an additive-proof case to
    test_golden_additive.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hotmem.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def client(tmp_path: Path):
    """A TestClient against a fresh temp DB — the golden baseline surface."""
    app = create_app(db_path=tmp_path / "golden.sqlite")
    with TestClient(app) as c:
        yield c


def mask(value: Any) -> Any:
    """Mask volatile values to typed sentinels, preserving structure/types.

    Recursively walks dicts/lists. Scalars become one of:
      "<int>", "<float>", "<str>", "<bool>", "<uuid>", "<hash>", "<ts>",
      "<path>", "<null>".
    Keys are always preserved (key drift is the real signal we want).
    """
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if value is None:
        return "<null>"
    if isinstance(value, bool):  # before int — bool is an int subclass
        return "<bool>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float>"
    s = str(value)
    if _looks_like_uuid(s):
        return "<uuid>"
    if _looks_like_hash(s):
        return "<hash>"
    if _looks_like_ts(s):
        return "<ts>"
    if _looks_like_path(s):
        return "<path>"
    return "<str>"


_UUID_RE = __import__("re").compile(r"^[0-9a-f]{32}$")


def _looks_like_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _looks_like_hash(s: str) -> bool:
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def _looks_like_ts(s: str) -> bool:
    # ISO-8601 UTC created_at, e.g. 2026-07-06T16:36:33Z
    return len(s) >= 8 and s[:4].isdigit() and "T" in s and s.endswith("Z")


def _looks_like_path(s: str) -> bool:
    return ("/" in s or "\\" in s) and s.endswith((".sqlite", ".jsonl", ".db"))


def assert_keys_exact(actual: dict, expected_keys: set[str], where: str) -> None:
    """Assert the dict has exactly ``expected_keys`` (no more, no fewer)."""
    actual_keys = set(actual)
    assert actual_keys == expected_keys, (
        f"{where}: key drift. expected {sorted(expected_keys)}, got {sorted(actual_keys)}"
    )


@pytest.fixture(scope="session")
def golden_cursor_vectors() -> dict[str, str]:
    """Locked opaque ``hmc1`` cursor tokens (full SHA-256 integrity digest)."""
    return {
        "start_v1": (
            "hmc1.eyJhbmNob3JfZXZlbnRfaWQiOiIiLCJiYXNlX3NuYXBzaG90X2lkIjoiIiwiaGlzdG9yeV9pZCI"
            "6Imhpc3QtMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDEiLCJtb3VudF9pZCI6Im1udC0wMDA"
            "wMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMSIsInJlcGxpY2FfaWQiOiJyZXAtMDAwMDAwMDAwMDA"
            "wMDAwMDAwMDAwMDAwMDAwMDAwMDEiLCJzY29wZSI6eyJldmVudF90eXBlcyI6WyJtZW1vcnkuY3JlYXR"
            "lZCJdLCJuYW1lc3BhY2UiOm51bGx9LCJzZXEiOjAsInYiOjF9.8411dc0734cf905a6095435f659f8e"
            "6b8a40cbf6212586d3eca4b6211e4df33c"
        ),
        "scoped_head_v1": (
            "hmc1.eyJhbmNob3JfZXZlbnRfaWQiOiJldnQtMDAwMyIsImJhc2Vfc25hcHNob3RfaWQiOiJzbmFwLWJ"
            "hc2UtMDEiLCJoaXN0b3J5X2lkIjoiaGlzdC0wMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMSI"
            "sIm1vdW50X2lkIjoibW50LTAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAxIiwicmVwbGljYV9"
            "pZCI6InJlcC0wMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMSIsInNjb3BlIjp7ImV2ZW50X3R"
            "5cGVzIjpbIm1lbW9yeS5jcmVhdGVkIl0sIm5hbWVzcGFjZSI6Im9wcyJ9LCJzZXEiOjMsInYiOjF9.80"
            "5a1b108aa9900c1dfaa60f7adc585ac89b76b79277fe3ef378b32007b08f5d"
        ),
    }
