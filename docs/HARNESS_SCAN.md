# Harness scan

`scripts/harness-scan.py` reviews the installation's agent permissions and other
executable surfaces before `self-deploy.sh` restarts the service. It is an offline
inventory and drift gate, not a sandbox or a claim that reviewed code is safe.
It neither imports scanned tools nor executes commands from configuration.

Run it as the install owner with the same overlay, module paths and backup/key
path overrides as the service and backup job:

```bash
uv run --offline --no-sync python scripts/harness-scan.py
# After a human has reviewed every reported change:
uv run --offline --no-sync python scripts/harness-scan.py --accept
```

`KBOTS_OVERLAY` (or `--overlay PATH`) is required. The baseline lives only at
`$KBOTS_OVERLAY/config/harness-baseline.json`, never in Core. `--engine PATH`
selects the installation to inspect; by default it is the scanner's own checkout.
`--module PATH` may be repeated, otherwise modules come from `KBOTS_MODULES`.
The root locations are bound into the baseline, so a different tree cannot
silently stand in for the installation.

## What is covered

- Core, configured modules and overlay agent `.claude/settings.json` and
  `settings.local.json`: allow and deny lists, env and every other setting.
  Permission lists are separate items. A changed allow list requires review for
  newly added or widened grants; removing a deny is a change too. Values are
  withheld from output, so inspect the named key in the local file when reviewing.
  Reordering allow/deny entries, config whitespace and comments are not drift.
- Each server in `config/mcp.yaml`, including transport, executable, arguments,
  URL, cwd and any additional fields. Each server is one item so changing its
  command or arguments produces one finding. Stdio needs an absolute executable
  or an exactly versioned `npx package@1.2.3` / `uvx package==1.2.3` invocation.
  Known package launchers require a pin even when the launcher path is absolute.
  Also supported: `npx --package=package@1.2.3 command`, `--package package@1.2.3`
  and `-p package@1.2.3`. Package options can repeat; every spec must be pinned.
  For uvx, use `--from package==1.2.3 command`, with optional repeated
  `--with extra==2.3.4` / `-w extra==2.3.4`. The long options also accept `=`,
  and uvx's positional `package@1.2.3` shorthand is supported. `--with` alone
  does not pin the main tool. Tags, ranges, wildcards and Git/URL requirements
  are rejected, including when mixed with pinned specs. Launcher options must
  precede the command (or `--`); later arguments belong to the application.
  Unknown launcher options, requirement files and shell-call forms are refused
  rather than guessed. The supported npx switches are `-y`, `--yes`, `--no`
  and `--no-install`. This checks direct package pins, not transitive dependency
  locking or package provenance, and never runs a launcher or resolves a package.
- Hooks in agent settings, agent `.codex/hooks.json`, and nested hook keys in
  `config/config.yaml`, `agents.yaml` and `security.yaml` in each layer.
  The scanner fingerprints configuration, not external hook executables' bodies.
- Root and agent `AGENTS.md` / `CLAUDE.md`, shared and agent `codex/`, and skills:
  credential prefixes, Discord webhook tokens, private-key headers and long,
  high-entropy assignments to key/token/secret names. Findings name a line and
  shape, never the matching value. Ordinary prose mentioning "key" is not a hit.
  Clean prose-only edits in prompt/codex files do not create drift; new/removed
  prompt files do. This gate is not a detector for every possible secret format.
- Complete file hashes for `skills/`, agent `skills/`, `tools/`, and Core
  `src/tools/`, including files overridden by a later layer. Git and Python/test
  caches are excluded. Skill/tool changes require review even without credentials.
- Only metadata for every `config/secrets.enc*`, the default vault/backup keys,
  configured key overrides, and files under the backup directory. Modes must be
  exactly `0600` and ownership must match the non-root engine install owner.
  Contents, sizes and modification times of secret files are never fingerprinted.
  The backup directory itself is a baseline item and must be a real directory
  with mode `0700` and the same owner, matching `backup.sh`. Each archive is
  checked on every scan for regular-file type, mode and ownership; archive names
  never enter the baseline. Correctly secured daily archives and retention
  pruning produce no drift. An unsafe archive, new or old, is an error that
  `--accept` cannot suppress. Nested backup files receive the same checks.
  Content-only rotations of the vault, keys and archives do not cause drift.
  The vault's atomic-write file `config/secrets.enc.tmp` is also checked without
  recording its transient name. The primary vault and keys remain baseline items.

Key defaults are `~/.config/kbots-vault-key` and `~/.config/kbots-backup-key`;
`KBOTS_VAULT_KEY_FILE` and `KBOTS_BACKUP_KEY_FILE` add their configured paths.
Backups use `KBOTS_BACKUP_DIR`, default `~/kbots-backups`, matching `backup.sh`.
The script cannot discover archives outside that configured directory or module
roots omitted from the deployment environment. No service configuration, shell
profile or scanned hook is executed to discover more paths.

Runtime `logs/`, `data/`, `tmp/`, agent workspace data/logs, Claude project
transcripts and Codex session records are not traversed or inventoried. Generated
Git/Python/test caches in the scanned source directories are excluded. New
settings, hooks, skills, tools and prompt/codex files still require review because
they change the harness surface. Do not put unrelated generated files in those
source directories. The backup location remains bound into the baseline; moving
or first creating the directory is a reviewable change, unlike archive rotation.
An older v1 baseline with per-archive entries needs one explicit acceptance of the
new directory item and removed archive entries; no approval is migrated silently.

## Review and acceptance

Exit `0` means unchanged or explicitly accepted. Exit `1` means drift or no
baseline. Exit `2` means an unsafe or unreadable surface, malformed configuration,
or a baseline error. The deploy treats all nonzero exits as failures.

First run prints the full inventory and fails. A normal scan never writes a
baseline. `--accept` prints every added, changed or removed location with old and
new SHA256 hashes before writing. The JSON contains only version, item locations
and hashes, with no settings, command lines, prompt text or detected credentials.
It is written atomically with mode `0600`; an unsuccessful write preserves the
previous baseline. Item locations are still private installation metadata.

Unsafe secret-file modes/owners, credential-shaped prompt text, unpinned commands,
unreadable files, symlinks, hard links and invalid/duplicate configuration keys
cannot be accepted. Fix these and scan again. Errors never include parser excerpts
that could disclose a value. Reads use descriptor-relative `O_NOFOLLOW` for every
component. Protected secret inodes and non-regular files are never read. Text
inputs are bounded to 8 MiB per file; incomplete scans fail instead of passing.
Acceptance scans twice to catch edits during review; it does not lock other
editors out. As with any deployment gate, subsequent writes can change the state.

`--accept` is an operator action. The scanner does not authenticate the human
behind an install-owner shell; it cannot defend a baseline from that same user.
Agents must not run it automatically to get a deployment past a red gate.

## Deployment

`self-deploy.sh` runs the scan after Ruff and pytest in the existing gate
environment, with `uv --offline --no-sync`. It runs before rename migration,
unit refresh and service restart. On rejection it rolls back the code and leaves
the running service alone; a rollback restart must not activate rejected rights.
Review the candidate and explicitly accept its surface before retrying deployment.
If rollback removed the scanner on its first installation, use the reviewed
scanner file with `--engine` pointing at the installation to create its first
baseline. Future candidate changes to skills/tools must be included in the
reviewed baseline before that candidate can pass.

The existing no-new-commit early exit and other deployment entrypoints are
unchanged. Run the scanner directly to review overlay changes without a new
engine commit. This patch supplies no baseline and approves no live surface.
