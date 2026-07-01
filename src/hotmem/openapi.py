"""HotMem OpenAPI spec export.

Purpose:
    Generate and export the FastAPI OpenAPI spec as static JSON/YAML so it can
    be versioned and published with the docs site, independent of a running
    server.

Interface:
    export_openapi() -> dict[str, Any]
    dump_openapi(path, fmt="json"|"yaml") -> Path
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hotmem.server import create_app


def export_openapi() -> dict[str, Any]:
    """Return the OpenAPI schema for the HotMem FastAPI app."""
    app = create_app(db_path=":memory:")
    return app.openapi()


def dump_openapi(path: str | Path, *, fmt: str = "json") -> Path:
    """Write the OpenAPI spec to a file. fmt is 'json' or 'yaml'.

    YAML support requires PyYAML; if absent, falls back to JSON with a warning
    and appends .json to the path.
    """
    path = Path(path)
    spec = export_openapi()

    if fmt == "yaml":
        try:
            import yaml
        except ImportError as err:
            raise ImportError(
                "YAML export requires PyYAML. Install with: uv pip install pyyaml"
            ) from err
        path.write_text(yaml.dump(spec, sort_keys=False, default_flow_style=False))
    else:
        path.write_text(json.dumps(spec, indent=2) + "\n")

    return path
