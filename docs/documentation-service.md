# Documentation Service

**Status:** Adopted build and deployment contract · **Owner:** HotMem maintainers

Scope: Public HotMem documentation, including crawler and LLM-readable sources.

## One-page decision

HotMem documentation is a **docs-as-code static service** built from this
repository. Markdown is the source of truth; MkDocs Material builds the site;
GitHub Pages delivers one immutable build artifact. There is no CMS, database,
runtime server, separate hosting vendor, or mandatory analytics service.

```text
versioned Markdown + MkDocs Material
  -> locked strict build on documentation pull requests
  -> immutable GitHub Pages artifact
  -> official Pages deployment from main
```

This keeps the public knowledge corpus reviewable beside the code and minimizes
the operational surface area. It is the delivery foundation for a standard that
must be discoverable by people, search engines, agents, and LLMs.

## Technology decision record

**Decision date:** 2026-08-02 · **Current choice:** Material for MkDocs 9.x ·
**Future migration candidate:** Zensical

HotMem will launch its public documentation with the locked Material for MkDocs
configuration already present in this repository. It fits the current product:
the sources and application are Python-native, `uv` already owns dependency
resolution, the output is static, local browser search needs no backend, and the
strict build and GitHub Pages workflow are implemented and tested together.

The choice is deliberately conservative rather than permanent. Zensical is the
preferred future migration candidate because it is developed by the Material
for MkDocs team, combines the generator and theme into a more vertically
integrated Rust/Python implementation, and provides a compatibility path for
existing Material projects and `mkdocs.yml`. As of this decision, Zensical is
still pre-1.0 and working toward complete feature parity, so changing the build
inside the initial deployment PR would add transition risk without improving
the public contract.

| Option | Fit for HotMem now | Decision |
| --- | --- | --- |
| Material for MkDocs | Already locked, Python/`uv` aligned, static, searchable, and passing strict CI | Use for the initial production service |
| Zensical | Promising compatible successor with a leaner, performance-oriented implementation | Re-evaluate after the migration gates below |
| VitePress or Astro Starlight | Strong static documentation products, but require a second Node/frontend toolchain | Do not add without a demonstrated requirement |
| Docusaurus | Mature and extensible, but its React/Node application surface exceeds the current need | Reject for the minimal service |

Authoritative project references:

- [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)
- [Zensical compatibility](https://zensical.org/compatibility/)
- [Zensical production and transition FAQ](https://zensical.org/docs/community/faqs/)
- [VitePress](https://vitepress.dev/guide/what-is-vitepress)
- [Astro Starlight](https://starlight.astro.build/)
- [Docusaurus](https://docusaurus.io/docs/)

### Zensical migration gates

Migration is a separate reviewed change and occurs only when all of the
following are true:

1. Every feature used by HotMem is supported without a compatibility workaround.
2. A pinned Zensical version has an acceptable maintenance and release posture.
3. A shadow build preserves the public routes, navigation, search, `llms.txt`,
   sitemap, Markdown rendering, and branded presentation.
4. Strict validation and GitHub Pages deployment remain at least as reliable as
   the current workflow.
5. Measured build time, artifact size, and maintenance burden are no worse, with
   a meaningful improvement in at least one of them.

Until those gates pass, Zensical is a watched migration path, not a second
generator, optional dependency, or dual-build requirement.

## Contract

### Source

- Documentation lives in `docs/`; `mkdocs.yml` defines information structure.
- `docs/llms.txt` is copied to the public site root as an LLM-readable source.
- Canon, current capability, protocol, migration, and security documents must
  link to each other and distinguish shipped behavior from north-star work.
- Python docs dependencies are declared in `pyproject.toml` and locked in
  `uv.lock`.

### Build gate

A pull request affecting documentation inputs runs:

```bash
uv run --locked --extra docs mkdocs build --strict
```

Strict mode turns broken links, invalid configuration, and documentation
warnings into a failed check. This is the only required documentation quality
gate at this stage.

### Deployment

Only a qualifying build from `main` is deployed. The workflow uses GitHub’s
official Pages actions to configure Pages, upload the generated static artifact,
and deploy it. Deployment permissions are isolated to the deploy job:

- build: repository read access;
- deploy: `pages: write` and `id-token: write`.

The repository’s GitHub Pages publishing source must be configured as **GitHub
Actions**. The standard public paths are:

- `/` — product entry point;
- `/vision-and-canon/` — durable north-star direction;
- `/agent-memory-portability/` — current portability and interoperability;
- `/snapshot-v2/` — snapshot contract;
- `/llms.txt` — compact crawler/LLM source.

## Deliberately excluded

- a separate documentation server or API;
- Node, React, Next.js, Astro, VitePress, Docusaurus, or another runtime stack;
- a CMS or hosted authoring system;
- preview-environment infrastructure;
- analytics, cookie banners, marketing automation, or a database;
- custom domain work.

Material’s built-in client-side search is retained: it keeps documentation
searchable without operating a search backend. Optional enhancements must earn
their complexity through a concrete user need.

## Future direction

The service remains intentionally minimal as content grows. The next meaningful
additions are content, not infrastructure: verified adapter guides, import and
export walkthroughs, clone and restore demonstrations, security protocol
documentation, and operational runbooks. A feature or hosting service is added
only when the static, repository-native model can no longer satisfy a proven
need.

This contract implements the Vision and Canon’s requirement that the HotMem
standard be discoverable, teachable, and durable.
