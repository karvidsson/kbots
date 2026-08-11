# Contributing to kbots

Thanks for your interest! kbots is a young project — issues, docs fixes, and
focused PRs are all welcome.

## Ground rules

- **All changes go through a PR.** Direct commits to `main` are blocked by a
  pre-commit hook, and every PR needs a maintainer review before merge.
- **Keep Core clean.** This repo is the engine only: no real config, no agent
  identities, no personal or company data, no domain-specific tools. Deployment
  content belongs in your own overlay ([ARCHITECTURE.md → Keeping Core
  Clean](ARCHITECTURE.md#keeping-core-clean)). Vendor integrations go in
  `extras/`, not `src/tools/`.
- **No secrets, names, or numeric IDs** in code, tests, or docs — use synthetic
  fixtures (`1000000000000000001`-style snowflakes, `example.com` addresses).

## Workflow

```bash
git checkout -b fix/short-description
# make your changes
uv run python scripts/dev.py check     # ruff + pytest — must pass
git commit -m "fix: short description"
git push -u origin fix/short-description
gh pr create
```

For engine changes, a quick way to test end-to-end without touching a live
install is the dev harness: `uv run python scripts/dev.py chat --mock`.

## Style

- Python 3.12+, type hints everywhere, minimal abstractions, no ORMs — raw SQL
  with small helpers.
- Tools are single `@tool`-decorated async functions; skills are YAML. Match
  the surrounding code's conventions.
