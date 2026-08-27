"""Shared helpers for the native helper spike (#48) harness.

Purpose:
     - Load native candidate helpers (C .so via ctypes, WASM via wasmtime)
       with a single "optional boundary": every loader raises
       NativeHelperUnavailable when the helper is missing, fails to build,
       or is disabled via HOTMEM_SPIKE_DISABLE_NATIVE=1. This mirrors the
       graceful-degradation contract any production helper must satisfy.
     - Memory and RSS measurement via /proc (resource.ru_maxrss is broken
       on this kernel — it reported ~11x the true VmHWM; see README).
     - Unprivileged cold-cache eviction via posix_fadvise(DONTNEED).

Deps: stdlib only for Python arms; ctypes + wasmtime are lazy imports.
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
CORPUS_DIR = SPIKE_DIR / "corpus"
MANIFEST_PATH = SPIKE_DIR / "manifest.json"

READ_CHUNK = 1 << 20
NATIVE_DISABLED = os.environ.get("HOTMEM_SPIKE_DISABLE_NATIVE", "") == "1"


class NativeHelperUnavailable(RuntimeError):
    """A native helper cannot be used; callers must fall back to Python."""


def _build(subdir: str) -> None:
    cmd = ["make", "-s", "-C", str(SPIKE_DIR / subdir)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise NativeHelperUnavailable(f"make -C {subdir} failed: {res.stderr.strip()}")


def _load_so(subdir: str, libfile: str) -> ctypes.CDLL:
    if NATIVE_DISABLED:
        raise NativeHelperUnavailable("native helpers disabled (HOTMEM_SPIKE_DISABLE_NATIVE=1)")
    so = SPIKE_DIR / subdir / libfile
    if not so.exists():
        _build(subdir)
    if not so.exists():
        raise NativeHelperUnavailable(f"{so} missing after build")
    try:
        return ctypes.CDLL(str(so))
    except OSError as err:
        raise NativeHelperUnavailable(f"cannot load {so}: {err}") from err


_checksum_lib: ctypes.CDLL | None = None


def c_checksum():
    """Range-hash helper: range_hash_pread / range_hash_mmap(path, off, len) -> hex.

    Returns (lib, call_pread, call_mmap) where the callables take
    (path, offset, length) and return the 64-char hex digest, raising
    NativeHelperUnavailable-style errors as ValueError with the C code.
    """
    global _checksum_lib
    if _checksum_lib is None:
        lib = _load_so("c_checksum", "librange_hash.so")
        for fn in ("range_hash_pread", "range_hash_mmap"):
            getattr(lib, fn).argtypes = [
                ctypes.c_char_p,
                ctypes.c_uint64,
                ctypes.c_uint64,
                ctypes.c_char_p,
            ]
            getattr(lib, fn).restype = ctypes.c_int
        _checksum_lib = lib
    lib = _checksum_lib
    buf = ctypes.create_string_buffer(65)

    def _call(fn: str, path: str, offset: int, length: int) -> str:
        rc = getattr(lib, fn)(path.encode(), offset, length, buf)
        if rc == -1:
            raise FileNotFoundError(path)
        if rc == -2:
            raise EOFError(f"truncated range in {path}")
        if rc != 0:
            raise OSError(f"{fn} failed with code {rc} on {path}")
        return buf.value.decode()

    return (
        lambda p, o, n: _call("range_hash_pread", p, o, n),
        lambda p, o, n: _call("range_hash_mmap", p, o, n),
    )


class _ScanResult(ctypes.Structure):
    _fields_ = [
        ("rows", ctypes.c_uint64),
        ("first_bad_index", ctypes.c_int64),
        ("first_bad_offset", ctypes.c_uint64),
        ("lines", ctypes.c_uint64),
        ("sample_count", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("sample_offsets", ctypes.c_uint64 * 16),
        ("sample_lengths", ctypes.c_uint64 * 16),
    ]


_scan_lib: ctypes.CDLL | None = None


def c_scan(path: str, *, sample_max: int = 5, validate: bool) -> dict:
    """C JSONL scanner: mode 0 = scan-only, mode 1 = scan + JSON validation."""
    global _scan_lib
    if _scan_lib is None:
        lib = _load_so("c_jsonl", "libscan.so")
        lib.scan_file.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.POINTER(_ScanResult),
        ]
        lib.scan_file.restype = ctypes.c_int
        _scan_lib = lib
    res = _ScanResult()
    rc = _scan_lib.scan_file(path.encode(), sample_max, 1 if validate else 0, ctypes.byref(res))
    if rc != 0:
        raise OSError(f"scan_file failed with code {rc} on {path}")
    return {
        "rows": res.rows,
        "lines": res.lines,
        "first_bad": None
        if res.first_bad_index < 0
        else {"index": res.first_bad_index, "offset": res.first_bad_offset},
        "first_n": [
            (res.sample_offsets[i], res.sample_lengths[i]) for i in range(res.sample_count)
        ],
    }


_wasm_scanner = None


def wasm_scanner():
    """Lazily compiled WAT scanner (wasmtime). Raises NativeHelperUnavailable."""
    global _wasm_scanner
    if _wasm_scanner is not None:
        return _wasm_scanner
    if NATIVE_DISABLED:
        raise NativeHelperUnavailable("native helpers disabled (HOTMEM_SPIKE_DISABLE_NATIVE=1)")
    try:
        import wasmtime
    except ImportError as err:
        raise NativeHelperUnavailable(f"wasmtime not installed: {err}") from err

    wat = (SPIKE_DIR / "wasm_parser" / "scanner.wat").read_text()
    try:
        engine = wasmtime.Engine()
        module = wasmtime.Module(engine, wat)
        store = wasmtime.Store(engine)
        instance = wasmtime.Instance(store, module, [])
        exports = instance.exports(store)
        scanner = _WasmScanner(store, exports)
    except Exception as err:  # wasmtime raises assorted config/compile errors
        raise NativeHelperUnavailable(f"wasmtime setup failed: {err}") from err
    _wasm_scanner = scanner
    return scanner


class _WasmScanner:
    """Host side of the WASM scanner: file I/O + carry, module does the loop."""

    DATA_BASE = 0x2000  # sample table occupies 0x1000..0x1100

    def __init__(self, store, exports) -> None:
        self._store = store
        self._exports = exports
        self._mem = exports["memory"]
        self._scan = exports["scan"]
        self._finish = exports["finish"]

    def scan_file(self, path: str, *, sample_max: int = 5) -> dict:
        store = self._store
        self._exports["reset"](store)
        if sample_max != 5:
            self._exports["set_sample_max"](store, sample_max)
        carry = b""
        offset = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(READ_CHUNK)
                if not chunk:
                    break
                data = carry + chunk
                self._ensure_capacity(store, len(data))
                self._mem.write(store, data, self.DATA_BASE)
                self._scan(store, self.DATA_BASE, len(data), offset - len(carry))
                last_nl = data.rfind(b"\n")
                carry = data[last_nl + 1 :]
                offset += len(chunk)
        self._finish(store, offset)
        count = self._exports["get_sample_count"](store)
        samples = [
            (
                self._exports["get_sample_offset"](store, i),
                self._exports["get_sample_length"](store, i),
            )
            for i in range(count)
        ]
        return {
            "rows": self._exports["get_rows"](store),
            "first_n": samples,
            "bytes": offset,
        }

    def _ensure_capacity(self, store, needed: int) -> None:
        """Grow linear memory when carry + chunk outgrows it (long lines)."""
        pages = self._mem.size(store)
        want = (self.DATA_BASE + needed + 0x10000) // 0x10000  # page-align + headroom
        if want > pages:
            self._mem.grow(store, want - pages)


# ---------------------------------------------------------------------- #
# Measurement helpers (/proc-based; ru_maxrss is unreliable on this box) #
# ---------------------------------------------------------------------- #


def _status_field(name: str) -> int | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(name):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def vm_rss_kb() -> int | None:
    return _status_field("VmRSS:")


def vm_hwm_kb() -> int | None:
    return _status_field("VmHWM:")


def try_reset_hwm() -> bool:
    """Best-effort VmHWM reset via clear_refs (works when self-writable)."""
    try:
        with open("/proc/self/clear_refs", "w") as f:
            f.write("5")
        return True
    except OSError:
        return False


def mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def fadvise_dontneed(path: str) -> None:
    """Best-effort page-cache eviction for one file (unprivileged)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def loadavg() -> str | None:
    try:
        with open("/proc/loadavg") as f:
            return f.read().split("\n")[0]
    except OSError:
        return None


def gcc_version() -> str | None:
    try:
        res = subprocess.run(["cc", "--version"], capture_output=True, text=True, timeout=10)
        return res.stdout.splitlines()[0] if res.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def host_info() -> dict:
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    sha_ni = False
    try:
        with open("/proc/cpuinfo") as f:
            sha_ni = " sha_ni" in f.read()
    except OSError:
        pass
    wasmtime_version = None
    try:
        import wasmtime

        wasmtime_version = getattr(wasmtime, "__version__", "unknown")
    except ImportError:
        pass
    return {
        "cpu": cpu,
        "sha_ni": sha_ni,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "gcc": gcc_version(),
        "wasmtime": wasmtime_version,
        "loadavg": loadavg(),
        "mem_available_mb": mem_available_mb(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def stats_ms(samples_s: list[float]) -> dict:
    """Median / max over per-run wall times (seconds in, ms out)."""
    ordered = sorted(samples_s)
    n = len(ordered)
    mid = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {
        "runs_ms": [round(s * 1000, 3) for s in samples_s],
        "median_ms": round(mid * 1000, 3),
        "p95_ms": round(ordered[-1] * 1000, 3) if ordered else None,
    }
