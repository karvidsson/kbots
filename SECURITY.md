# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Instead, use
GitHub's private vulnerability reporting on this repository (Security →
"Report a vulnerability"), and include reproduction steps if you can.

You should get an acknowledgement within a few days. Please allow a reasonable
window for a fix before disclosing publicly.

## Scope notes

- kbots executes tools **in-process** — the AST validation on agent-created
  tools is a filter, not a sandbox. Reports about escaping it are welcome, but
  running untrusted agents on a trusted machine is outside the threat model.
- Secrets belong in the Fernet vault. Any path that causes a secret to be
  written to disk unencrypted, logged, or sent to a chat channel is a
  vulnerability — report it.
- The inbound webhook connector and the HITL approval gate are security
  boundaries; bypasses of either are in scope.
