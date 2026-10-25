# Daemonite desktop ownership, tray interface and packaging

Approved direction: 25 October 2026. Target: HotMem v0.3.0. Implementation and platform qualification remain pending.

## Product ownership and theme

Daemonite is HotMem's desktop console. The HotMem repository owns its screens, interaction model, tray/window lifecycle, domain action binding, assets and release packaging. Use HotMem's documented KnowGuard-inspired color and typography roles as the initial theme. Keep theme tokens separate from components and live status semantics, and retain the monochrome tray marks appropriate to the operating-system surface.

Product documentation, packages, issues and releases describe Daemonite in HotMem/KnowGuard terms. General design inspiration remains an implementation detail rather than a named renderer dependency. The desktop application uses the same authenticated operation boundary as other clients; HotMem core remains usable headlessly.

## Tray interaction

Use one tray-anchored utility panel, initially approximately 380-420 logical pixels wide. This is a starting design size, not a fixed acceptance metric.

- Show daemon health, the selected brain/store and observed connection freshness.
- Show attached harness/session state that HotMem actually observes.
- Provide bounded memory search, recent activity and permitted common operations; deeper management can open an expanded window.
- Clicking the tray toggles the panel. Escape or focus loss dismisses it; an explicit Pin control keeps it open. Preserve active confirmation/input handling.
- Reuse the window after first opening and pause unnecessary refresh while hidden. Dismissing the panel leaves the daemon and other clients running.
- Provide an Open Daemonite tray-menu path where direct tray interaction or positioning is unavailable. Linux tray behavior requires platform-specific observation.

## Desktop stack and installation baseline

Use Tauri 2 for the Rust tray/window shell and a small bundled static HTML/CSS/TypeScript interface. The native boundary exposes bounded application actions and calls the authenticated local HotMem API; the UI does not open SQLite independently.

Retain the existing Python daemon. For a self-contained desktop installation, begin with a private Python runtime and companion executable packaged in a one-folder layout inside the platform installer. PyInstaller is the initial packaging candidate. Define daemon start/connect/recovery independently of popup visibility; bundled-executable support alone does not establish the required daemon lifecycle.

Build and qualify artifacts per supported OS/architecture. Candidate formats are macOS app/DMG, Windows installer and selected Linux packages. Record the first supported desktop target through an installed run rather than assume the full matrix. Account for signing/notarization, Windows WebView2 installation policy and Linux webview/tray dependencies. Keep optional semantic/vector packages out of the base desktop bundle unless its declared capabilities require them.

Core/runtime/daemon/CLI/desktop share the HotMem product release version under the existing policy. Toolchain and dependency versions remain independently pinned. No package bump or publication follows from this memo.

## First installed proof and remaining uncertainty

Install the app, start or connect the selected daemon/store, open real state from the tray, perform one permitted memory operation, then dismiss/reopen the panel while another client keeps working. Stop at the first concrete platform/packaging blocker, or when that path succeeds.

Measure cold startup, warm panel opening, total process memory, idle CPU and compressed/installed size from that run. The Rust shell, system WebView, Python daemon and selected extras all contribute to footprint. No numerical size, latency or memory guarantee is accepted here.

This adds concrete desktop direction to the v0.3.0 epic. Existing v0.2.7 acceptance, safe memory swaps, relay reconnect, denial/recovery checks and final release evidence remain in their own trackers.

## Technical references

- [Tauri system tray](https://v2.tauri.app/learn/system-tray/)
- [Tray-relative positioning](https://v2.tauri.app/plugin/positioner/)
- [Webview versions](https://v2.tauri.app/reference/webview-versions/)
- [Bundled external binaries](https://v2.tauri.app/develop/sidecar/)
- [Platform distribution](https://v2.tauri.app/distribute/)
- [Windows WebView2 installation](https://v2.tauri.app/distribute/windows-installer/)
- [PyInstaller operating modes](https://pyinstaller.org/en/stable/operating-mode.html)
