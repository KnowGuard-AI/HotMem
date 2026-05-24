# Contributing to HotMem

Thanks for your interest in contributing! HotMem is a small, focused project and we want to keep it that way.

## Development Setup

```bash
git clone https://github.com/KnowGuard-AI/HotMem.git
cd HotMem
uv sync
uv run pytest           # tests
uv run ruff check src/ tests/   # lint
uv run ruff format src/ tests/  # format
```

Requires Python 3.11+.

## Pull Requests

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Ensure tests pass: `uv run pytest`
4. Ensure lint passes: `uv run ruff check src/ tests/`
5. Open a PR against `main`

Keep PRs small and focused. One concern per PR.

## Code Style

- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting
- Line length: 100
- Target: Python 3.11
- Every module has a docstring header describing purpose, interface, deps, and extension points

## What We're Looking For

- Bug fixes with a test that reproduces the issue
- Documentation improvements
- Framework integration adapters (see [issue tracker](https://github.com/KnowGuard-AI/HotMem/issues))

## Design Principles

HotMem is deliberately small. Before proposing a new feature, check that it aligns with:

- **Local-first**: no external service dependencies
- **Zero-dep core**: stdlib + FastAPI/Click/httpx only
- **Single-file DB**: SQLite, nothing else
- **LLM-ready output**: search returns message objects

If your idea requires adding heavy dependencies or external services, it may be a better fit for the broader KnowGuard ecosystem rather than HotMem core.

## Agentic Contributors

If you are a code agent (Copilot, Cursor, Oz, Claude Code, etc.), follow these rules on top of everything above.

### Formatting

- No em dashes. Use `--` or rephrase the sentence
- No smart/curly quotes. ASCII only in code, comments, docstrings, and docs
- No trailing whitespace or mixed indentation

### Code Discipline

- Do not add dependencies. HotMem is zero-dep core (stdlib + FastAPI/Click/httpx). If you think you need one, stop and ask
- Do not create new files. HotMem is deliberately small. Extend existing modules unless explicitly told otherwise
- Do not refactor module boundaries. Each module is self-contained by design
- Every module must keep its docstring header (purpose, interface, deps, extension points)
- Use `trace.py` for logging, never `print()` or stdlib `logging`
- No `# type: ignore` or `# noqa` without an inline explanation

### PR Discipline

- No placeholder/TODO comments in submitted code
- Every new behavior needs a test
- Run the full suite before submitting: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run pytest`
- Conventional commit messages (`feat:`, `fix:`, `chore:`, `docs:`)
- One concern per PR. Do not bundle unrelated changes

### API Contract

- Search always returns LLM-ready message objects. Do not break this contract
- All endpoints live under `/v1`. Do not add routes outside this prefix
- SQLite only. Do not introduce other storage backends

## Reporting Issues

Use the [GitHub issue templates](https://github.com/KnowGuard-AI/HotMem/issues/new/choose) for bug reports and feature requests.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
