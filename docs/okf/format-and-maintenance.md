# OKF: Format and Maintenance

Status: Draft
Owner: HotMem maintainers
Last updated: 2026-07-06
Scope: Development documentation practices

## 1. Purpose

HotMem organizes repository documentation in an OKF-style format by default.
This format is intentionally lightweight. It gives each document enough
structure to preserve decisions, rationale, thresholds, and open questions
without pretending every idea is final.

Public docs such as quickstart, API reference, and CLI reference should remain
direct and useful, but still carry OKF metadata and maintenance sections.
Planning and development knowledge should go under `docs/okf/` first, then
graduate into public docs or API references once stable.

## 2. Required Shape

Each OKF note should start with:

- A title beginning with `OKF:`.
- `Status`.
- `Owner`.
- `Last updated`.
- `Scope`.

Each OKF note should then include, as relevant:

- Purpose.
- Current decisions.
- Compatibility rules.
- Heuristics or thresholds.
- HotMem owns vs out-of-scope boundaries.
- Risks.
- Open questions.
- Recommended next steps.

## 3. Status Values

Use simple status labels:

- `Draft`: active thinking; useful but expected to change.
- `Accepted`: current working decision for implementation.
- `Superseded`: kept for history, no longer current.
- `Archived`: historical note, not part of active planning.

## 4. Maintenance Rules

- Do not delete useful discussion just because it is not final.
- Prefer adding a dated update or superseding note over rewriting history.
- Keep compatibility requirements visible near the top of planning docs.
- Separate heuristics from hard requirements.
- Move stable user-facing behavior into the regular docs when implementation
  lands.
- Keep OKF docs plain markdown so they remain easy to read outside the docs
  site.

## 5. Documentation Defaults

New docs should use OKF shape unless there is a strong reason not to.

Default flow:

1. Start active planning in `docs/okf/`.
2. Track implementation work in GitHub issues.
3. When an OKF decision stabilizes, update the relevant public doc.
4. If a public doc contains architecture rationale, extract that rationale
   into an OKF note and link both ways.
5. Keep quickstart/API/CLI docs concise even though they are OKF-shaped.

This keeps the repository moving without losing older documentation or forcing
premature structure onto unfinished ideas.

## 6. GitHub Issue Relationship

GitHub issues are the active implementation tracker. OKF docs preserve context,
decisions, and heuristics.

Do not duplicate every acceptance criterion in docs once an issue exists.
Instead:

- Link or name the issue set.
- Keep docs focused on why the direction exists.
- Keep issues focused on what must be implemented and verified.
- Update OKF notes when issue outcomes change the underlying decision.
