"""Python baseline arms for the native helper spike (#48).

Purpose:
     Benchmark arms that reproduce the CURRENT hotmem Python paths, plus the
     pure-Python improvement candidates they would be compared against.

     B1 (range checksum):
       py_current_double_read — the real hydrate-with-verify path:
           adapter.read_range() then verify_range() (which re-reads the range).
           Uses the REAL hotmem classes, exactly as memory.hydrate_memory_detailed
           calls them (src/hotmem/memory.py:244 + src/hotmem/provenance.py:103).
       py_single_read — candidate pure-Python fix: one read, hash once.
       py_streaming — best-case pure Python: chunked seek+read+hash, no
           whole-range bytes object (mirrors storage/local.py _checksum style).

     B2 (JSONL scanning):
       py_stream_real — the REAL JSONLInspector._stream (validation included).
       py_scan_only — replica of _stream's scan loop with validation removed,
           used to isolate scanning cost from json.loads validation cost.

Note on offsets:
     py_scan_only uses corrected line-start arithmetic (base + pos). The
     shipped _stream computes line_start = offset + pos in (carry+chunk)
     coordinates, which overstates offsets by len(carry) whenever a line spans
     a read-chunk boundary. See README ("Discovered: _stream offset bug").

Deps: hotmem (real code paths), stdlib only.
"""

from __future__ import annotations

import hashlib
import json

READ_CHUNK = 1 << 20  # 1 MiB — same window as JSONLInspector._stream.
_WS = b" \t\n\r\v\f"


def py_current_double_read(uri: str, offset: int, length: int, expected: str) -> str:
    """Faithful reproduction of the current verified-hydration path.

    Reads the range (read 1, held while verifying — as memory.py does), then
    verify_range() re-reads the same bytes and hashes them (read 2). Raises
    ProvenanceError subclasses on failure, exactly like production.
    """
    from hotmem.provenance import verify_range
    from hotmem.storage.local import LocalFilesystemAdapter

    adapter = LocalFilesystemAdapter()
    data = adapter.read_range(uri, offset, length)  # read 1 — kept alive, as in memory.py
    try:
        verify_range(adapter, uri, offset, length, expected)  # read 2 + hash
    finally:
        del data
    return expected


def py_single_read(uri: str, offset: int, length: int) -> str:
    """Candidate pure-Python fix: one read, hash once."""
    from hotmem.storage.local import LocalFilesystemAdapter

    adapter = LocalFilesystemAdapter()
    data = adapter.read_range(uri, offset, length)
    try:
        return hashlib.sha256(data).hexdigest()
    finally:
        del data


def py_streaming(path: str, offset: int, length: int) -> str:
    """Best-case pure Python: chunked read + incremental hash, O(1MB) memory."""
    h = hashlib.sha256()
    remaining = length
    with open(path, "rb") as f:
        f.seek(offset)
        while remaining > 0:
            chunk = f.read(min(READ_CHUNK, remaining))
            if not chunk:
                raise EOFError(f"short read: {length - remaining} bytes missing")
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _nonblank(line: bytes) -> bool:
    return bool(line.strip(_WS))


def py_scan_only(path: str, *, sample_size: int = 5) -> dict:
    """Scan-only contract: rows + first-N line boundaries, no JSON validation.

    Mirrors JSONLInspector._stream's chunk/carry structure and its sampling
    eligibility gate (line_index < sample_size), but validates nothing.
    Line offsets use corrected arithmetic (see module docstring).
    """
    rows = 0
    first_n: list[tuple[int, int]] = []
    line_index = 0
    carry = b""

    with open(path, "rb") as f:
        offset = 0
        while True:
            chunk = f.read(READ_CHUNK)
            if not chunk:
                if carry and _nonblank(carry):
                    rows += 1
                    if line_index < sample_size:
                        first_n.append((offset - len(carry), len(carry)))
                break
            data = carry + chunk
            base = offset - len(carry)  # file offset of data[0]
            pos = 0
            nl = data.find(b"\n", pos)
            while nl != -1:
                line = data[pos : nl + 1]
                if _nonblank(line):
                    rows += 1
                    if line_index < sample_size:
                        first_n.append((base + pos, nl + 1 - pos))
                line_index += 1
                pos = nl + 1
                nl = data.find(b"\n", pos)
            carry = data[pos:]
            offset += len(chunk)

    return {"rows": rows, "first_n": first_n, "bytes": offset}


def py_stream_real(path: str, *, sample_size: int = 5) -> dict:
    """Run the REAL JSONLInspector._stream and normalize its outputs.

    Returns row_count, sample rows, byte_ranges, and the first-bad-line
    (index, offset) parsed out of the unsupported_reason message (the real
    function only reports it as a human string).
    """
    from hotmem.inspectors.jsonl_inspector import _stream  # noqa: PLC2701 — bench parity target

    row_count, sample_rows, byte_ranges, unsupported_reason = _stream(
        path, count_rows=True, sample_size=sample_size
    )
    bad = None
    if unsupported_reason:
        prefix = "line "
        rest = unsupported_reason[len(prefix) :]
        index_s, rest = rest.split(" (offset ", 1)
        offset_s = rest.split(")", 1)[0]
        bad = {"index": int(index_s), "offset": int(offset_s), "reason": unsupported_reason}
    return {
        "row_count": row_count,
        "sample_rows": sample_rows,
        "byte_ranges": byte_ranges,
        "first_bad": bad,
    }


def reference_range_digest(path: str, offset: int, length: int) -> str:
    """Independent reference digest for parity assertions (plain hashlib)."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    if len(data) != length:
        raise EOFError(f"short read: got {len(data)}, want {length}")
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    """Quick smoke check on the real repo swap.jsonl + one corpus file."""
    from pathlib import Path

    corpus = Path(__file__).resolve().parent / "corpus"
    swap = Path(__file__).resolve().parents[2] / "swap.jsonl"
    if swap.exists():
        r = py_stream_real(str(swap))
        s = py_scan_only(str(swap))
        assert r["row_count"] == s["rows"], (r["row_count"], s["rows"])
        print(f"swap.jsonl: rows={r['row_count']} scan_first5={s['first_n'][:2]}")
    binf = corpus / "bin_1mb.bin"
    if binf.exists():
        ref = reference_range_digest(str(binf), 0, binf.stat().st_size)
        assert py_single_read(str(binf), 0, binf.stat().st_size) == ref
        assert py_streaming(str(binf), 0, binf.stat().st_size) == ref
        assert py_current_double_read(str(binf), 0, binf.stat().st_size, ref) == ref
        print(f"bin_1mb.bin: B1 arms agree with reference {ref[:16]}…")
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()
