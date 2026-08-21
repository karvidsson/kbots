# Codex — Business Knowledge Directory

This directory holds **stable business knowledge that no API can serve** — the background an agent needs to make sense of your business: brand identity, strategy, relationships, compliance, and processes.

## The Golden Rule

One question gates every addition: **"Could an agent fetch this from an API or tool instead?"**

| Answer | Action |
|--------|--------|
| **Yes** — an API, database, or tool holds the data | Keep it out of the codex. If the right tool isn't obvious, leave an API pointer. |
| **No, but it shifts frequently** | Store a snapshot carrying a "last updated" date and a refresh cadence. |
| **No, and it rarely changes** | It belongs in the codex. |

## Structure

```
codex/
├── _index.md              <- Master index — agents read this first
├── README.md              <- This file (how to use the codex)
├── business/              <- Brand voice, company profile, compliance
├── products/              <- Product info, data sheets, guidelines
├── strategy/              <- Playbooks, competitor analysis
└── processes/             <- SOPs, style guides, workflows
```

Treat the layout above as a suggestion — arrange things however suits your business.

## How It Works

1. **Everything starts at `_index.md`.** It is the first file an agent opens, telling it what knowledge exists and where each piece lives. Keep it up to date.

2. **The codex is wired in through each agent's CLAUDE.md.** An agent's CLAUDE.md points at whichever codex sections matter for its role — brand voice and competitor briefs for a marketing agent, say, or contacts and processes for an ops agent.

3. **Whether to read the codex or hit an API is decided per query.** Because `_index.md` spells out which data comes from APIs and which from codex files, an agent won't fall back on stale codex content when a live source exists.

## Shared vs Per-Agent Codex

There are two codex levels, both injected at session start (`src/core/startup_context.py`):

- **Shared codex** — this directory (or `$KBOTS_OVERLAY/codex/` in a deployment). Knowledge every agent needs: company profile, brand voice, compliance. Its `_index.md` is injected as `<codex-index>`.
- **Per-agent codex** — `agents/<agent>/codex/` inside the agent's own project directory. Role-specific knowledge only that agent cares about: an ops agent's runbooks, a marketing agent's campaign briefs. Its `_index.md` is injected as `<agent-codex-index>`. New agents get a starter one scaffolded automatically.

Put knowledge at the narrowest level that needs it: shared if two or more agents use it, per-agent otherwise.

## Adding Content

### New document

1. Write the file into whichever subdirectory fits
2. Include a "Last updated: YYYY-MM-DD" line close to the top
3. Register it in `_index.md` under the matching section
4. Where the content is volatile, record how often it should be refreshed

### Updating existing documents

1. Revise the content
2. Bump the "Last updated" date
3. Should an API now cover the data, swap the content for an API pointer

### Removing documents

Once codex content gains API coverage, the document shrinks to a pointer:
```
~~product-pricing.md~~ -> use `my_pricing_tool` instead
```

## What Does NOT Belong Here

- **Live data** (prices, stock, metrics) -> fetch through tools
- **Anything git already records** -> reach for `git log` / `git blame`
- **Conversation context** -> that's what the agent memory system is for
- **Credentials or secrets** -> the encrypted vault holds those

## Tips

- One topic per file. Small, focused documents beat sprawling ones.
- Structured data (contacts, registries) reads best as markdown tables.
- Put a date on everything — an undated entry can't be trusted.
- Point, don't copy. A note like "see `my_tool` for current data" outlives any table that would be stale within a week.
