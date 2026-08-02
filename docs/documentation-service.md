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
