# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in HotMem, please report it responsibly.

**Email:** security@knowguard.ai

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

We will acknowledge receipt within 48 hours and aim to provide a fix or mitigation within 7 days for critical issues.

## Scope & Distinct Security Guarantees

HotMem is a local-first memory runtime designed to run on loopback (`127.0.0.1`). It separates six distinct security and privacy guarantees so operators never conflate one with another:

1. **API Authorization (`src/hotmem/policy.py`):** In daemon policy mode (`--policy` or `<mount>/policy.json`), every operation is authenticated via bearer token (`hmac.compare_digest` over SHA-256 token hashes) and scoped to explicit `store_id`, `namespaces`, `operations`, `allowed_sessions`, `allowed_sources`, and `ShareApproval` grants. Cross-namespace access defaults to deny.
2. **Outbound Redaction & Omission (`secret_action`, `private_classification_action`):** Policy rules can `allow`, `redact`, `omit`, or `fail_closed` when secret-shaped values, PII patterns, or private-classified records are encountered across retrieval and exports, emitting machine-readable `disclosure` counts/reasons without echoing sensitive substrings.
3. **Explicit Handoff Consent (`src/hotmem/handoff/`):** Session handoff preparation (`hotmem-handoff-v1`) requires an explicit non-empty `consent` string checked before reading source session files, applies 3-layer redaction with a fail-closed output gate, and marks historical tool calls inert (`executable=false`). A regular scoped snapshot (`POST /v1/snapshot`) is governed by credential/share policy and does not substitute for a consented session handoff.
4. **Artifact Integrity (SHA-256):** Snapshot v2, `hotmem-interchange-v1` packages, and `hotmem-handoff-v1` packages carry SHA-256 content hashes and manifest digests verified fail-closed before hydration. Checksums detect corruption or tampering; they do not hide content or prove key-bound authorship.
5. **Encryption at Rest / in Transit:** HotMem stores plaintext SQLite and JSONL artifacts and serves HTTP over loopback. Confidentiality against host-level readers or network observers requires OS filesystem permissions, disk/archive encryption, or an authenticated TLS/overlay tunnel.
6. **Cryptographic Signing:** Manifests use deterministic SHA-256 digests (`snapshot_id`, `logical_id`, `package_id`), not asymmetric cryptographic signatures.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅        |
| 0.1.x   | ✅        |
