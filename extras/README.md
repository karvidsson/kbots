# extras/ — opt-in tools, not loaded by Core

Tools in here are **not on the discovery path**. `Registry.discover()` scans
`src/tools/`, `$KBOTS_MODULES/*/tools/`, and `$KBOTS_OVERLAY/tools/` — never
`extras/`. Nothing in this directory runs until you copy it into a deployment.

## Why

Core ships the engine: the agent's interface to kbots itself (create_tool,
memory, team, schedules, HITL, the message bus) plus generic primitives
(http_request, web_search, browser, computer). Integrations that assume you own
a particular account or a particular piece of hardware are not that — they are
bloat for every install that doesn't have them. Those live here, reviewed and
tested, and get installed per deployment.

The line, in order of increasing specificity:

| Layer | Holds | Path |
|---|---|---|
| Core `src/tools/` | engine API + generic primitives | this repo, always loaded |
| `extras/` | curated integrations, opt-in | this repo, **never** loaded |
| Layer 2 | domain tools shared across deployments | `$KBOTS_MODULES` |
| Overlay | what this deployment actually runs | `$KBOTS_OVERLAY/tools/` |

Agent-authored tools (`create_tool`) always land in the overlay, private to
their creator until `promote_tool`. That path is unchanged.

## Catalog

| Extra | Tools | Needs |
|---|---|---|
| `google/` | Gmail, Calendar, Meet, Drive (15) + `debrief` skill | Google OAuth2 |
| `trello/` | boards, lists, cards, activity (8) | API key + token |
| `notion/` | search, read, create | API key |
| `github/` | issues, PRs, comment (6) | token |
| `cloudflare/` | zones, dns_list, dns_update | API token |
| `gemini/` | video/image analysis, image gen | API key |
| `stocks/` | quotes, history, fundamentals, technicals (8) + `financial_analyst` skill | none |
| `monitoring/` | rss_read, web_watch, weather | none |
| `news/` | news_search, news_feeds | `monitoring/` installed |
| `shelly/` | smart-home switch/dim/cover control | devices on LAN |

## Installing an extra

```bash
cp extras/<name>/<name>.py "$KBOTS_OVERLAY/tools/"
```

Tools hot-load — `create_tool` reloads live, and a restart picks up anything
copied in manually. Read the extra's README first: most need a config block.

Overlay copies do not track this repo. When an extra changes here, re-copy it.

## Adding an extra

```
extras/<name>/
  <name>.py         the module — imports from src.core.* work unchanged
  test_<name>.py    tests, collected by CI via testpaths in pyproject.toml
  conftest.py       puts the dir on sys.path so `import <name>` resolves
  README.md         config block, credentials, security notes
```
