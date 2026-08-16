# Publishing Core — the flip-to-public checklist

Core is developed in a private repo and published deliberately. This is the
procedure, plus the standing defenses that keep install specifics out of the
repo between publishes. It exists because the repo has been recreated twice
after installation-identifying text reached surfaces no working-tree cleanup
can fix.

## The threat model, in one paragraph

Installation specifics (agent names, human names, account IDs, channel and
guild IDs, hostnames, paths) leak through four surfaces: the working tree,
commit messages, PR titles/bodies, and GitHub-side residue. The first two
travel with every clone. The last one is the trap: **GitHub keeps immutable
`refs/pull/*` for every PR head and a viewable edit history for every PR
body** — no force-push or edit ever removes them. A repo that leaked once
can be cleaned for future clones but never fully scrubbed in place.

## Standing defenses (already wired — keep them working)

| Layer | What | Bypassable? |
|-------|------|-------------|
| Local hooks (`.githooks/`, `core.hooksPath`) | pre-commit blocks direct `main` commits; commit-msg scans messages against the local overlay roster | Yes — `--no-verify`. Which is why the workflow docs forbid it entirely |
| Test suite | `tests/test_no_install_leaks.py` — tree, branch commit messages | Only by not running tests |
| CI (`.github/workflows/ci.yml`) | Same checks on every PR and push, **plus** the PR title/body scan. Roster names arrive via the `KBOTS_LEAK_NAMES` repo secret, so CI checks names without an overlay and without the list ever entering the tree | No — if merges go through `gh pr merge --auto` |
| Merge ritual | `gh pr merge --auto --merge` — merge lands only after CI is green | Discipline until branch protection exists (see below) |

### The `KBOTS_LEAK_NAMES` secret

A newline-separated list of names that identify this deployment: agent
names/slugs, human first/last/full names, anything the roster knows. Set it
from a machine that has the overlay:

```bash
python3 - <<'EOF' | gh secret set KBOTS_LEAK_NAMES --repo <owner>/kbots
import json, os
from pathlib import Path
team = json.loads((Path(os.environ["KBOTS_OVERLAY"]) / "config/team.json").read_text())
names = set()
for m in team.get("agents", []) + team.get("humans", []):
    for f in ("id", "name"):
        v = str(m.get(f) or "").strip()
        if len(v) >= 4:
            names.add(v)
print("\n".join(sorted(names)))
EOF
```

Re-run whenever an agent or human joins the roster. Fork PRs never see
secrets — they degrade to shape-checks only, which is fine: outsiders don't
know your names.

## Flip-day procedure

1. **Full sweep** on a machine with the overlay:
   ```bash
   FIRST=$(git rev-list --max-parents=0 HEAD | tail -1)
   python3 scripts/check_install_leaks.py --tree
   python3 scripts/check_install_leaks.py --commits $FIRST..HEAD
   # plus: scan every PR title/body via `gh pr list --json` and --text
   ```
   All three must be clean. If commit history is dirty, rewrite it
   (`git-filter-repo --replace-message/--replace-text`) BEFORE publishing.
2. **Publish to a FRESH repository — never flip the dev repo public.**
   The dev repo's `refs/pull/*` and PR edit histories contain every
   pre-cleanup leak forever. Push the verified history to a new (or
   deleted-and-recreated) repo: no PRs, no edit history, no residue —
   clean by construction.
   ```bash
   git clone --mirror <private-repo> /tmp/publish && cd /tmp/publish
   # ... verify sweep, rewrite if needed ...
   git push <public-repo-url> 'refs/heads/main' 'refs/tags/*'
   ```
3. **Enable branch protection** on the public repo (available once public;
   private repos need a paid plan):
   ```bash
   gh api -X PUT repos/<owner>/<repo>/branches/main/protection \
     -f 'required_status_checks[strict]=true' \
     -f 'required_status_checks[contexts][]=lint + tests' \
     -f 'required_status_checks[contexts][]=no install leaks in PR title/body' \
     -F 'enforce_admins=true' \
     -F 'required_pull_request_reviews=null' \
     -F 'restrictions=null' \
     -F 'allow_force_pushes=false'
   ```
   From then on the leak checks are enforced server-side — `--no-verify`
   and hasty merges can no longer land anything unchecked.
4. **Set `KBOTS_LEAK_NAMES`** on the public repo (command above).
5. Day-to-day development continues in the private repo; publish by pushing
   `main` to the public repo (fast-forward only — protection blocks force).

## If a leak lands anyway

1. Fix the tree by PR as usual — that's the visible part.
2. If it reached a commit message or blob: rewrite with `git-filter-repo`
   and force-push the private repo (needs owner approval).
3. If it reached a PR title/body: edit the body AND remember the edit
   history survives — treat the private repo as tainted for publishing and
   rely on the fresh-repo publish step to shed it.
4. Extend `scripts/check_install_leaks.py` with whatever shape or name the
   guard missed, with a test proving the miss is closed. Every rule in that
   file exists because something real got through.
