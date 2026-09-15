"""Optional local semantic embedder — the [semantic] extra (issue #78).

Purpose:
    One reference implementation of the portable `Embedder` protocol backed
    by a static-embedding model (model2vec): tiny, deterministic, fast cold
    start, and fully offline. This module is importable only when the
    optional ``hotmem[semantic]`` extra is installed; the config resolver
    turns the missing import into an actionable install hint.

Provisioning contract:
    The runtime loads an explicitly provisioned local artifact directory.
    It never downloads a model at import, startup, hydration, or test time,
    and never executes remote model code. The artifact directory must
    contain:

      - the model2vec model files (produced by ``StaticModel.save_pretrained``)
      - ``hotmem-model.json`` — the pinned identity the administrator
        provisioned: ``{"model": "...", "revision": "...", "preprocessing":
        "..."}``. The pin makes the stored descriptor auditable and stable;
        it is the artifact's declared identity, not a discovery.

Interface:
    LocalSemanticEmbedder(model_path) -> Embedder (protocol-compliant)

Deps: model2vec (optional [semantic] extra), hotmem.embed
Extension: hosted adapters implement the same protocol elsewhere; this
    module stays the one lean local reference.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

try:
    import model2vec  # noqa: F401 — availability gate for the [semantic] extra
except ImportError as _err:
    raise ImportError(
        "local-semantic embedder requires the optional [semantic] extra. "
        "Install it with: uv pip install 'hotmem[semantic]'"
    ) from _err

from hotmem.embed import EmbeddingDescriptor

_PIN_NAME = "hotmem-model.json"


class LocalSemanticEmbedder:
    """Static-embedding adapter over an explicitly provisioned local artifact.

    The descriptor is pinned from the artifact's ``hotmem-model.json``
    (implementation ``local-m2v``): key stability is what makes stored vectors
    reusable across machines — the artifact, the pin, and the descriptor all
    move together. Vectors are L2-normalized on the way out so the stored
    space always satisfies the descriptor's normalization policy.
    """

    def __init__(self, model_path: str | Path) -> None:
        import model2vec as m2v

        path = Path(model_path)
        if not path.is_dir():
            raise ValueError(
                f"local-semantic model path is not a directory: {path}. "
                "Provision the model artifact locally (see docs/retrieval-quality.md); "
                "HotMem never downloads models."
            )
        pin_path = path / _PIN_NAME
        if not pin_path.is_file():
            raise ValueError(
                f"local-semantic model artifact is missing {_PIN_NAME} in {path}. "
                "The pin file declares the provisioned identity: "
                '{"model": "...", "revision": "...", "preprocessing": "..."}.'
            )
        try:
            pin: dict[str, Any] = json.loads(pin_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise ValueError(f"invalid {_PIN_NAME} in {path}: not valid JSON ({err})") from err
        model_name = str(pin.get("model") or "").strip()
        revision = str(pin.get("revision") or "").strip()
        preprocessing = str(pin.get("preprocessing") or "").strip()
        if not model_name or not revision:
            raise ValueError(
                f"invalid {_PIN_NAME} in {path}: 'model' and 'revision' are required, "
                f"'preprocessing' optional (got model={model_name!r}, revision={revision!r})"
            )

        self._model = m2v.StaticModel.load_pretrained(str(path))
        probe = self._model.encode(["dimension probe"])
        dimension = len(probe[0])
        if dimension <= 0:
            raise ValueError(f"local-semantic model produced an empty vector (dim {dimension})")

        self._descriptor = EmbeddingDescriptor(
            implementation="local-m2v",
            model=model_name,
            dimension=dimension,
            revision=revision,
            normalization="l2",
            metric="cosine",
            preprocessing=preprocessing or "default",
        )

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(self, text: str) -> list[float]:
        vec = self._model.encode([text])[0]
        values = [float(x) for x in vec]
        norm = math.sqrt(sum(x * x for x in values))
        if norm > 0:
            values = [x / norm for x in values]
        return values
