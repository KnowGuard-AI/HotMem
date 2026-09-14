"""Path confinement primitives for untrusted archive/manifest paths.

Purpose:
     One rule shared by the Snapshot v2 verifier and the interchange package
     verifier (#67 §9, #69): a manifest- or archive-listed path may never make
     HotMem read or write outside its root. Absolute paths, ``..`` traversal,
     and symlink escapes are rejected BEFORE any file is touched.

     Lives in hotmem.interchange (not hotmem.snapshot) so both readers can
     import it without import cycles: interchange never imports snapshot.

Interface:
      confined_relpath(root, rel) -> bool

Deps: stdlib only.
Extension: archive extraction (#69 attachments) should reuse this helper.
"""

from __future__ import annotations

import os
from pathlib import Path


def confined_relpath(root: Path, rel: str) -> bool:
    """True if ``rel`` names a path inside ``root`` without traversal/symlinks.

    A crafted manifest must never make the verifier read outside the package
    (interchange contract #67 §9). Absolute paths and any component escaping
    the root are rejected; symlinked entries are rejected because they can
    point outside even with a clean relative name.
    """
    if not rel or rel.startswith("/") or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return False
    resolved = (root / rel).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return False
    return os.path.realpath(root / rel) == str(resolved) and not os.path.islink(root / rel)
