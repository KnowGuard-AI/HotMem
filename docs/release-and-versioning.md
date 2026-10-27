# Release, Versioning, and Packaging Policy

Approved on 2026-10-25. Package versions identify published releases. Ordinary
feature, fix, documentation, and chore PRs can merge without a version bump;
several PRs can contribute to one release. A focused urgent fix may justify its
own release.

Implementation of the version/packaging checks is tracked in
[#129](https://github.com/KnowGuard-AI/HotMem/issues/129); the Daemonite feature
release is tracked in [#130](https://github.com/KnowGuard-AI/HotMem/issues/130).

## Release sequence

| Target | Commitment |
| --- | --- |
| `0.2.7` | Preserve the already planned showcase, shared daemon, portable-workspace work, and minimal management/detachable interface in [#112](https://github.com/KnowGuard-AI/HotMem/issues/112). Existing acceptance still applies. |
| Later `0.2.x` | Compatible fixes and maintenance, published when useful. `0.2.8` is available for such a release; it is not a mandatory intermediate release. |
| `0.3.0` | Next planned feature release: complete Daemonite tray/pop-up integration, common memory operations, safe memory swaps, and supported system relay controls. |
| `1.0.0` | A deliberate commitment to a documented stable public API and compatibility contract. |

The existing `0.2.7` feature/showcase target is retained as an explicit transition
exception. After that target, patch increments communicate compatible fixes and
maintenance; minor increments communicate new capabilities. Before `1.0.0`, an
incompatible public API change requires a minor increment, migration notes, and
an explicit compatibility decision. Released artifacts are immutable; corrected
contents require a new version.

## Product versions and compatibility

Use `pyproject.toml` as the authoritative HotMem product version. The Python
distribution, runtime, daemon/CLI, and desktop distribution report that same
product release. Platform build identifiers may supplement it.

TypeScript clients and ecosystem adapters remain separately versioned packages.
Publish them when their own changes require a release and declare the supported
HotMem range and tested core/adapter combinations. Their version numbers do not
need to match the core product number. A difference alone is not a defect.

Record the renderer dependency by its own pinned version or revision. Protocol,
record, snapshot, interchange, and identity-algorithm versions are separate
compatibility contracts. Change them only when their contracts require it, with
the corresponding reader behavior and migration or rejection rules.

## Release PR and package cut

1. Merge the intended implementation PRs and record their changes under
   `Unreleased`. Identify remaining acceptance and compatibility limits.
2. Prepare one release PR that aligns the authoritative version, runtime
   `__version__`, `uv.lock` editable-root entry, release notes, and the metadata
   of every included product artifact. Record independently versioned packages
   and dependency pins in the release compatibility mapping.
3. Build and install the intended artifacts. Compare installed Python metadata,
   CLI/daemon version reporting, and the intended tag. When a desktop/container
   artifact is included, check its version, core compatibility, and source
   revision against the same release mapping. Verify published dependency and
   platform claims using the declared supported combinations.
4. Review and merge the release PR, then verify the tag, publication, artifact
   identities, and release links. Development builds use a source revision or
   distinct prerelease identifier rather than replacing a published version.

Today, changing `pyproject.toml` on `main` invokes the auto-tag workflow. If its
version has no existing tag, the workflow creates `v<version>` and dispatches
the PyPI release workflow. Treat the release PR merge as publication intent.
The existing workflow checks Python metadata/runtime/tag consistency; broader
lockfile, desktop, container, and adapter checks remain implementation work
until their evidence is recorded. This policy does not itself bump a package,
publish a release, or prove those additional checks exist.

## Daemonite integration boundary

Daemonite is HotMem's planned desktop interface: a persistent system tray entry
and Docker-inspired click-to-open mini pop-up. HotMem owns its interface,
KnowGuard-inspired theme, tray integration, daemon lifecycle, store identity,
permissions, execution, and durable memory. The [desktop direction](daemonite-desktop.md)
records the Tauri 2/Rust shell, static TypeScript UI and Python packaging baseline.
The interface returns typed actions to the authenticated daemon boundary.

The first slice installs/starts or connects to HotMem, opens the tray pop-up,
shows the selected store and observed client state, performs one permitted
operation, and dismisses the pop-up while the daemon and clients continue.
The full feature release adds the committed common-operation map, safe memory
switching, and supported relay lifecycle controls with actual backend results.
Record the first desktop target, supported platforms, and failure/recovery
limits from observed runs.

Memory swaps, verified context handoff, native provider-session continuation,
and cross-device synchronization have distinct contracts. A tray control does
not establish all of them. Existing v0.2.7 acceptance remains in its own release.

Product docs, issues, packages, and release notes use HotMem/KnowGuard ownership
and product language. Private organizational provenance stays out of those
surfaces. Record actual shipping dependencies and their license obligations.
The core remains headless and independent of the desktop interface.
