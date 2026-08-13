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
  This applies to **everything you write, not just the files you change**:
  comments, test fixtures, **commit messages** and **pull request
  descriptions** all count. That is not a style note — it is where every leak
  so far has actually happened, and text outside the tree is the expensive kind:
  a file can be edited, a commit message needs a history rewrite, and a PR body
  stays on GitHub after the branch is gone.

  `scripts/check_install_leaks.py` is the single implementation, run from three
  places so nothing is checked only by good intentions:

  | Surface | Enforced by |
  |---|---|
  | tracked files | `tests/test_no_install_leaks.py` |
  | commit messages | the same tests, plus `.githooks/commit-msg` |
  | PR title and body | `.github/workflows/ci.yml` |

  It fails on real snowflakes, email addresses, home-directory paths and tailnet
  hostnames — checks that need no configuration and work in a fresh public
  clone — and, on a machine with an overlay, on that deployment's own agent
  names. Enable the hook once per clone with `git config core.hooksPath
  .githooks`; it is the cheapest place to catch this, but the test suite is the
  guarantee, since `--no-verify` skips hooks and `scripts/self-deploy.sh` will
  not ship a red suite.

## Workflow

```bash
git checkout -b fix/short-description
# make your changes
uv run python scripts/dev.py check     # ruff + pytest — must pass
git commit -m "fix: short description"
git push -u origin fix/short-description
gh pr create
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary`, with types `feat`, `fix`, `docs`, `chore`, `refactor`,
`test`, `perf`, `ci` — matching the branch-name prefixes above.

For engine changes, a quick way to test end-to-end without touching a live
install is the dev harness: `uv run python scripts/dev.py chat --mock`.

## Style

- Python 3.12+, type hints everywhere, minimal abstractions, no ORMs — raw SQL
  with small helpers.
- Tools are single `@tool`-decorated async functions; skills are YAML. Match
  the surrounding code's conventions.
