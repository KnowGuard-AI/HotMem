"""Explicit local model provisioning for the optional semantic embedder.

Purpose:
    HotMem never downloads a model at import, startup, hydration, search, or
    test time. This module is the one place a download may happen, and only
    when an operator runs ``hotmem model fetch``. It fetches a model from a
    small pinned registry, verifies every file against a recorded sha256,
    writes the ``hotmem-model.json`` identity pin, and stages the complete
    artifact before publishing it into the local models directory.

Interface:
    KNOWN_MODELS / DEFAULT_MODEL - the pinned registry
    models_dir() / default_model_path(name?) - where artifacts live
    fetch_model(name?, dest?, downloader?, force?) -> FetchResult
    verify_model(path) -> ModelStatus - pin + checksum state of an artifact
    resolve_provisioned_model_path() -> Path | None - default artifact, if valid

Deps: stdlib; huggingface_hub only inside the default downloader (it ships
    with the [semantic] extra and is imported lazily).
Extension: add a registry entry with its pinned commit and file checksums.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hotmem.trace import get_tracer

_trace = get_tracer("models")

PIN_NAME = "hotmem-model.json"
MODELS_DIR_ENV = "HOTMEM_MODELS_DIR"


@dataclass(frozen=True)
class ModelSpec:
    """One pinned, checksum-verified model artifact.

    ``pin`` is the declared identity written to ``hotmem-model.json``; it
    determines the stored embedding descriptor, so it must never change for a
    given set of weights. ``commit`` and ``files`` pin the exact bytes.
    """

    name: str
    repo: str
    commit: str
    files: dict[str, str]
    pin: dict[str, str]
    dimension: int
    size_mb: int


KNOWN_MODELS: dict[str, ModelSpec] = {
    "potion-base-8M": ModelSpec(
        name="potion-base-8M",
        repo="minishlab/potion-base-8M",
        commit="bf8b056651a2c21b8d2565580b8569da283cab23",
        files={
            "config.json": "2a6ac0e9aaa356a68a5688070db78fc3a464fefe85d2f06a1905ce3718687553",
            "model.safetensors": "f65d0f325faadc1e121c319e2faa41170d3fa07d8c89abd48ca5358d9a223de2",
            "tokenizer.json": "e67e803f624fb4d67dea1c730d06e1067e1b14d830e2c2202569e3ef0f70bb50",
        },
        # Same identity as the artifact behind bench/retrieval/semantic-local-m2v.json,
        # so stores embedded before `hotmem model fetch` existed stay compatible.
        pin={"model": "potion-base-8M", "revision": "v1", "preprocessing": "m2v-static-v1"},
        dimension=256,
        size_mb=30,
    ),
}
DEFAULT_MODEL = "potion-base-8M"

# (spec, filename) -> local path of the downloaded file.
Downloader = Callable[[ModelSpec, str], Path]


class ModelError(ValueError):
    """A model could not be fetched or failed verification."""


@dataclass(frozen=True)
class ModelStatus:
    """Verification state of one artifact directory."""

    path: Path
    provisioned: bool
    pin: dict[str, str] = field(default_factory=dict)
    known: bool = False
    verified: bool = False
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "provisioned": self.provisioned,
            "pin": dict(self.pin),
            "known": self.known,
            "verified": self.verified,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class FetchResult:
    name: str
    path: Path
    already_present: bool
    files: int
    bytes: int


def models_dir() -> Path:
    """Directory holding provisioned artifacts (``HOTMEM_MODELS_DIR`` or ``~/.hotmem/models``)."""
    override = os.environ.get(MODELS_DIR_ENV)
    return Path(override).expanduser() if override else Path.home() / ".hotmem" / "models"


def default_model_path(name: str = DEFAULT_MODEL) -> Path:
    return models_dir() / name


def _spec(name: str) -> ModelSpec:
    try:
        return KNOWN_MODELS[name]
    except KeyError:
        raise ModelError(
            f"unknown model {name!r}; known models: {', '.join(sorted(KNOWN_MODELS))}"
        ) from None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(path: str | Path) -> ModelStatus:
    """Report whether ``path`` holds a pinned artifact and whether its bytes verify.

    An artifact provisioned by hand (pin present, files not from the registry
    download) is ``provisioned`` but not ``verified``: it is still usable, and
    the report says its bytes were not checked rather than implying they were.
    """
    root = Path(path)
    pin_path = root / PIN_NAME
    if not root.is_dir() or not pin_path.is_file():
        return ModelStatus(path=root, provisioned=False, problems=(f"missing {PIN_NAME}",))
    try:
        raw = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return ModelStatus(path=root, provisioned=False, problems=(f"unreadable pin: {err}",))
    if not isinstance(raw, dict) or not raw.get("model") or not raw.get("revision"):
        return ModelStatus(
            path=root, provisioned=False, problems=("pin lacks 'model' or 'revision'",)
        )
    pin = {str(key): str(value) for key, value in raw.items()}
    spec = next((s for s in KNOWN_MODELS.values() if s.pin == pin), None)
    if spec is None:
        return ModelStatus(path=root, provisioned=True, pin=pin)
    problems = []
    for filename, expected in spec.files.items():
        target = root / filename
        if not target.is_file():
            problems.append(f"missing {filename}")
        elif _sha256(target) != expected:
            problems.append(f"{filename} does not match the pinned sha256")
    return ModelStatus(
        path=root,
        provisioned=True,
        pin=pin,
        known=True,
        verified=not problems,
        problems=tuple(problems),
    )


def resolve_provisioned_model_path() -> Path | None:
    """Return the default artifact directory when one is provisioned, else None."""
    path = default_model_path()
    status = verify_model(path)
    if not status.provisioned:
        return None
    if status.known and not status.verified:
        raise ModelError(
            f"the provisioned {DEFAULT_MODEL} artifact failed checksum verification: "
            f"{'; '.join(status.problems)}; rerun `hotmem model fetch --force`"
        )
    return path


def _hub_downloader(spec: ModelSpec, filename: str) -> Path:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as err:
        raise ModelError(
            "fetching a model requires the optional [semantic] extra. "
            "Install it with: uv pip install 'hotmem[semantic]'"
        ) from err
    try:
        return Path(hf_hub_download(spec.repo, filename, revision=spec.commit))
    except Exception as err:  # network, auth, missing file: all one actionable failure
        raise ModelError(
            f"could not download {filename} for {spec.name} from {spec.repo}@{spec.commit[:12]}: "
            f"{type(err).__name__}: {err}"
        ) from err


def fetch_model(
    name: str = DEFAULT_MODEL,
    dest: str | Path | None = None,
    *,
    downloader: Downloader | None = None,
    force: bool = False,
) -> FetchResult:
    """Download, verify, and publish one registry model.

    Idempotent: a destination that already verifies is left untouched unless
    ``force``. Every file is checked against its pinned sha256 before
    publication; a mismatch leaves the destination exactly as it was. A forced
    replacement stages first and restores the previous artifact if publishing
    the replacement fails.
    """
    spec = _spec(name)
    target = Path(dest) if dest is not None else default_model_path(name)
    download = downloader or _hub_downloader

    if not force and verify_model(target).verified:
        size = sum((target / filename).stat().st_size for filename in spec.files)
        return FetchResult(spec.name, target, True, len(spec.files), size)
    if target.exists() and not force:
        raise ModelError(
            f"{target} exists but is not a verified {spec.name} artifact; "
            "pass --force to replace it or choose another --dest"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{spec.name}.", dir=target.parent))
    backup: Path | None = None
    try:
        total = 0
        for filename, expected in spec.files.items():
            source = download(spec, filename)
            actual = _sha256(source)
            if actual != expected:
                raise ModelError(
                    f"checksum mismatch for {filename} of {spec.name}: expected {expected}, "
                    f"got {actual}; nothing was installed"
                )
            shutil.copyfile(source, staging / filename)
            total += (staging / filename).stat().st_size
        (staging / PIN_NAME).write_text(
            json.dumps(spec.pin, sort_keys=True) + "\n", encoding="utf-8"
        )
        if target.exists():
            backup = target.with_name(f".{target.name}-previous-{uuid.uuid4().hex}")
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except BaseException:
            if backup is not None and backup.exists():
                os.replace(backup, target)
                backup = None
            raise
        if backup is not None:
            try:
                if backup.is_dir() and not backup.is_symlink():
                    shutil.rmtree(backup)
                else:
                    backup.unlink()
            except OSError as err:
                _trace.warn(
                    "publish",
                    "new model was installed but the previous artifact could not be removed",
                    detail={"error": type(err).__name__},
                )
            backup = None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    _trace.info("fetch", f"provisioned {spec.name}", detail={"path": str(target), "bytes": total})
    return FetchResult(spec.name, target, False, len(spec.files), total)
