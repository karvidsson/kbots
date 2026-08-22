#!/usr/bin/env python3
"""Apply the memory overhaul to data that already exists.

Every fix in src/memory and src/lib/graph_store changes what happens on the
next write. None of them touch what is already stored, and on a mature
deployment that is most of it: the live fleet graph had 167 entities holding
190 edges across 71 distinct relation names, with duplicate spellings of the
same thing sitting side by side and no anchors joining any of it to the 234
memories in sqlite. Shipping the code alone would leave the old corpus
fragmented forever and make the improvement invisible.

Five passes, each independently skippable, each reporting what it would do:

  relations   collapse synonym relation names onto the shared vocabulary
  timestamps  give pre-existing edges a valid_from so history is orderable
  entities    merge entities that differ only by case, punctuation or spacing
  supersede   close older values of single-valued facts that have several
  anchors     link memories to the entities they mention, both directions
  embeddings  compute the vectors that were never generated

Dry run by default. Nothing is written without --apply, and --apply refuses to
run while another process holds either store open, because both are
single-writer and the running engine is the other writer.

    uv run scripts/memory-backfill.py                # report only
    uv run scripts/memory-backfill.py --apply        # after stopping the service
"""

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.lib.canonical import SINGLE_VALUED, entity_key, normalize_rel  # noqa: E402
from src.lib.graph_store import GraphMemory, _rows  # noqa: E402

PASSES = ("relations", "timestamps", "entities", "supersede", "anchors", "embeddings")

# An entity name shorter than this matches too much prose to anchor on. "AI"
# appears in half the corpus and tells you nothing about which memory is about
# the entity AI.
MIN_ANCHOR_LEN = 4


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _holders(path: Path) -> list[str]:
    """PIDs holding `path` open, via lsof. Empty when nothing does."""
    if not path.exists():
        return []
    try:
        out = subprocess.run(["lsof", "-t", "--", str(path)], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in out.stdout.split() if p and p != str(os.getpid())]


class Report:
    """Counts plus the first few examples, so a dry run is readable."""

    def __init__(self):
        self.counts: dict[str, int] = defaultdict(int)
        self.examples: dict[str, list[str]] = defaultdict(list)

    def note(self, kind: str, example: str | None = None, n: int = 1):
        self.counts[kind] += n
        if example and len(self.examples[kind]) < 8:
            self.examples[kind].append(example)

    def render(self) -> str:
        if not self.counts:
            return "  nothing to change"
        lines = []
        for kind in sorted(self.counts):
            lines.append(f"  {self.counts[kind]:>5}  {kind}")
            for ex in self.examples[kind]:
                lines.append(f"         {ex}")
        return "\n".join(lines)


# --- graph passes ---------------------------------------------------------

async def pass_relations(conn, apply: bool, rep: Report) -> None:
    """Rewrite relation names onto the canonical vocabulary."""
    rows = _rows(await conn.execute(
        "MATCH (a:Entity)-[r:Related]->(b:Entity) "
        "RETURN a.name AS src, r.rel AS rel, b.name AS dst"))
    for row in rows:
        old = row["rel"]
        new, known = normalize_rel(old)
        if new == old:
            continue
        rep.note("relations renamed" if known else "relations normalised (off-vocabulary)",
                 f"{row['src']} —{old}→ {row['dst']}   becomes  {new}")
        if apply:
            await conn.execute(
                "MATCH (a:Entity {name: $a})-[r:Related]->(b:Entity {name: $b}) "
                "WHERE r.rel = $old SET r.rel = $new",
                {"a": row["src"], "b": row["dst"], "old": old, "new": new})


async def pass_timestamps(conn, apply: bool, rep: Report) -> None:
    """Backfill valid_from from created_at on edges that predate the column."""
    rows = _rows(await conn.execute(
        "MATCH (a:Entity)-[r:Related]->(b:Entity) WHERE r.valid_from IS NULL "
        "RETURN a.name AS src, r.rel AS rel, b.name AS dst, "
        "r.created_at AS created_at"))
    for row in rows:
        stamp = row.get("created_at") or _now()
        rep.note("edges given a valid_from",
                 f"{row['src']} —{row['rel']}→ {row['dst']}   valid_from {stamp}")
        if apply:
            await conn.execute(
                "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity {name: $b}) "
                "WHERE r.valid_from IS NULL SET r.valid_from = $ts",
                {"a": row["src"], "b": row["dst"], "rel": row["rel"], "ts": stamp})


def _pick_canonical(names: list[str], degree: dict[str, int]) -> str:
    """Which spelling of a duplicated entity survives.

    Most-connected first, because that is the spelling the rest of the graph
    already points at and keeping it means rewriting fewer edges. Ties break on
    the longer name ("Dr. Zoid" over "Dr.Zoid" — spacing is more likely to be
    the considered form than its absence), then alphabetically so the choice is
    deterministic and a dry run predicts the real run exactly.
    """
    return sorted(names, key=lambda n: (-degree.get(n, 0), -len(n), n))[0]


async def pass_entities(conn, apply: bool, rep: Report) -> None:
    """Merge entities whose names differ only by case, punctuation or spacing."""
    names = [r["name"] for r in _rows(
        await conn.execute("MATCH (e:Entity) RETURN e.name AS name")) if r.get("name")]

    degree: dict[str, int] = defaultdict(int)
    for row in _rows(await conn.execute(
            "MATCH (a:Entity)-[r:Related]->(b:Entity) "
            "RETURN a.name AS src, b.name AS dst")):
        degree[row["src"]] += 1
        degree[row["dst"]] += 1

    groups: dict[str, list[str]] = defaultdict(list)
    for name in names:
        key = entity_key(name)
        if key:
            groups[key].append(name)

    for key, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        canonical = _pick_canonical(group, degree)
        losers = [n for n in group if n != canonical]
        rep.note("entities merged",
                 f"{' + '.join(repr(n) for n in losers)}  into  {canonical!r}")
        if not apply:
            continue

        for loser in losers:
            # Re-point every edge, in both directions, then drop the node. A
            # self-edge would be created if canonical and loser were already
            # connected, so those are dropped rather than pointed at
            # themselves.
            for out_edge in _rows(await conn.execute(
                    "MATCH (a:Entity {name: $a})-[r:Related]->(b:Entity) "
                    "RETURN r.rel AS rel, b.name AS dst, r.confidence AS confidence, "
                    "r.scope AS scope, r.created_by AS created_by, "
                    "r.created_at AS created_at, r.valid_from AS valid_from, "
                    "r.valid_to AS valid_to", {"a": loser})):
                if out_edge["dst"] != canonical:
                    await _recreate(conn, canonical, out_edge["dst"], out_edge)
            for in_edge in _rows(await conn.execute(
                    "MATCH (a:Entity)-[r:Related]->(b:Entity {name: $b}) "
                    "RETURN r.rel AS rel, a.name AS src, r.confidence AS confidence, "
                    "r.scope AS scope, r.created_by AS created_by, "
                    "r.created_at AS created_at, r.valid_from AS valid_from, "
                    "r.valid_to AS valid_to", {"b": loser})):
                if in_edge["src"] != canonical:
                    await _recreate(conn, in_edge["src"], canonical, in_edge)

            await conn.execute(
                "MATCH (a:Entity {name: $n})-[r:Related]->(:Entity) DELETE r", {"n": loser})
            await conn.execute(
                "MATCH (:Entity)-[r:Related]->(b:Entity {name: $n}) DELETE r", {"n": loser})
            await conn.execute("MATCH (e:Entity {name: $n}) DELETE e", {"n": loser})

        # The losing spellings stay on the survivor as aliases: a merge that
        # cannot be audited is indistinguishable from data loss.
        existing = _rows(await conn.execute(
            "MATCH (e:Entity {name: $n}) RETURN e.aliases AS aliases", {"n": canonical}))
        parts = [p for p in ((existing[0].get("aliases") if existing else "") or "").split("|") if p]
        for loser in losers:
            if loser not in parts:
                parts.append(loser)
        await conn.execute(
            "MATCH (e:Entity {name: $n}) SET e.aliases = $aliases, e.entity_key = $key",
            {"n": canonical, "aliases": "|".join(parts[:20]), "key": key})


async def _recreate(conn, src: str, dst: str, edge: dict) -> None:
    """Point an edge at the canonical endpoint, unless it is already there."""
    existing = _rows(await conn.execute(
        "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity {name: $b}) "
        "RETURN COUNT(*) AS n", {"a": src, "b": dst, "rel": edge["rel"]}))
    if int(existing[0]["n"]) > 0:
        return
    await conn.execute(
        "MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) "
        "CREATE (a)-[r:Related {rel: $rel, confidence: $conf, scope: $scope, "
        "created_by: $by, created_at: $created, valid_from: $vf, valid_to: $vt}]->(b)",
        {"a": src, "b": dst, "rel": edge["rel"],
         "conf": float(edge.get("confidence") or 0.7),
         "scope": edge.get("scope") or "global", "by": edge.get("created_by") or "",
         "created": edge.get("created_at") or _now(),
         "vf": edge.get("valid_from") or edge.get("created_at") or _now(),
         "vt": edge.get("valid_to")})


async def pass_supersede(conn, apply: bool, rep: Report) -> None:
    """Close older values where a single-valued fact has several current ones."""
    rows = _rows(await conn.execute(
        "MATCH (a:Entity)-[r:Related]->(b:Entity) WHERE r.valid_to IS NULL "
        "RETURN a.name AS src, r.rel AS rel, b.name AS dst, "
        "r.valid_from AS valid_from, r.created_at AS created_at"))
    by_subject: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["rel"] in SINGLE_VALUED:
            by_subject[(row["src"], row["rel"])].append(row)

    ts = _now()
    for (src, rel), group in sorted(by_subject.items()):
        if len(group) < 2:
            continue
        # Newest wins. Where nothing is dated, the tie breaks alphabetically so
        # the outcome is reproducible rather than dependent on scan order.
        group.sort(key=lambda r: (r.get("valid_from") or r.get("created_at") or "",
                                  r["dst"]), reverse=True)
        keep, close = group[0], group[1:]
        rep.note("contradictions closed",
                 f"{src} {rel} → {keep['dst']} (kept), "
                 f"closed: {', '.join(c['dst'] for c in close)}")
        if apply:
            for edge in close:
                await conn.execute(
                    "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->"
                    "(b:Entity {name: $b}) WHERE r.valid_to IS NULL SET r.valid_to = $ts",
                    {"a": src, "rel": rel, "b": edge["dst"], "ts": ts})


# --- sqlite pass ----------------------------------------------------------

async def pass_anchors(conn, memory, apply: bool, rep: Report) -> None:
    """Anchor existing memories to the entities their text mentions.

    The reflector anchors as it extracts, so new memories arrive joined to the
    graph. Everything stored before that lands with no anchors at all, which
    means the graph hop in recall finds nothing for the majority of the corpus.
    Matching entity names against memory text is a weaker signal than the
    reflector's extraction, so it is deliberately conservative: whole words
    only, and nothing shorter than four characters.
    """
    names = [r["name"] for r in _rows(
        await conn.execute("MATCH (e:Entity) RETURN e.name AS name")) if r.get("name")]
    patterns = [(n, re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.IGNORECASE))
                for n in names if len(entity_key(n)) >= MIN_ANCHOR_LEN]
    if not patterns:
        return

    have = {(r[0], r[1]) for r in memory.db.execute(
        "SELECT memory_id, entity_key FROM memory_entities").fetchall()}

    for row in memory.db.execute("SELECT id, content FROM memories").fetchall():
        content = row["content"] or ""
        hits = [n for n, pat in patterns if pat.search(content)]
        fresh = [n for n in hits if (row["id"], entity_key(n)) not in have]
        if not fresh:
            continue
        rep.note("memories anchored",
                 f"{row['id'][:8]}… → {', '.join(sorted(fresh)[:5])}")
        rep.note("anchors written", n=len(fresh))
        if apply:
            await memory.anchor_entities(row["id"], fresh)


async def pass_embeddings(memory, apply: bool, rep: Report) -> None:
    """Compute the vectors for memories stored while the model was missing.

    Until the download path was fixed this was every memory ever stored: the
    live fleet had 237 memories and 0 embeddings, so semantic search, whose
    query filters `WHERE embedding IS NOT NULL`, could only ever return
    nothing. The engine half of recall was not weak, it was absent.
    """
    from src.core.embedding import EmbeddingEngine

    rows = memory.db.execute(
        "SELECT id, content FROM memories WHERE embedding IS NULL").fetchall()
    if not rows:
        return
    rep.note("memories with no vector", n=len(rows))
    if not apply:
        rep.note("memories with no vector",
                 f"(dry run does not load the model; {len(rows)} would be embedded)", n=0)
        return

    done = 0
    for row in rows:
        try:
            blob = EmbeddingEngine.to_blob(memory.embedding.embed_one(row["content"]))
        except Exception as e:
            # One reason, once. If the model cannot load, it will not load for
            # row 2 either, and 237 identical tracebacks help nobody.
            print(f"  embeddings: stopped after {done} — {e}")
            break
        memory.db.execute("UPDATE memories SET embedding = ? WHERE id = ?",
                          (blob, row["id"]))
        done += 1
    memory.db.commit()
    rep.note("vectors written", n=done)


# --- driver ---------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: report only)")
    ap.add_argument("--db", default=None, help="path to memory.db")
    ap.add_argument("--graph", default=None, help="path to the graph store")
    ap.add_argument("--only", action="append", choices=PASSES,
                    help="run only this pass (repeatable)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the pre-apply copy of the graph store")
    args = ap.parse_args()

    # Both stores resolve through memory_config, the one helper that anchors
    # them to data_dir. Reading the config keys directly here is what put the
    # engine and its own maintenance scripts on two different databases.
    from src.core.base import PROJECT_ROOT, memory_config
    from src.main import load_config

    cfg = load_config()
    mem_cfg = memory_config(cfg)
    db_path = Path(args.db or mem_cfg["path"])
    graph_path = Path(args.graph or (mem_cfg.get("graph") or {}).get("path")
                      or (PROJECT_ROOT / "data" / "graph" / "memory.lbdb"))
    if not graph_path.is_absolute():
        graph_path = PROJECT_ROOT / graph_path

    passes = args.only or list(PASSES)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] memory: {db_path}")
    print(f"[{mode}] graph:  {graph_path}")
    print(f"[{mode}] passes: {', '.join(passes)}\n")

    if args.apply:
        for label, path in (("memory.db", db_path), ("graph", graph_path)):
            held = _holders(path)
            if held:
                print(f"REFUSING: {label} is open by pid(s) {' '.join(held)}.\n"
                      f"Both stores are single-writer. Stop the engine first.")
                return 2
        if not args.no_backup and graph_path.exists():
            backup = graph_path.with_name(
                graph_path.name + "." + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".bak")
            if graph_path.is_dir():
                shutil.copytree(graph_path, backup)
            else:
                shutil.copy2(graph_path, backup)
            print(f"backup: {backup}\n")

    from src.memory.sqlite import SQLiteMemory
    memory = SQLiteMemory(config={**mem_cfg, "path": str(db_path)})

    # Opened only when a pass needs it. Opening the graph takes its single
    # writer lock even to read, so `--only embeddings` must not touch it: that
    # pass works entirely inside sqlite and is the one worth being able to run
    # without a stopped window.
    graph = conn = None
    if set(passes) - {"embeddings"}:
        graph = GraphMemory({"enabled": True, "path": str(graph_path)})
        try:
            conn = await graph._ensure_open()
        except RuntimeError as e:
            if "lock" not in str(e).lower():
                raise
            print(f"REFUSING: the graph store is locked by another process.\n"
                  f"  {graph_path}\n"
                  f"Stop the engine first (launchctl stop com.kbots.agent), or run\n"
                  f"  --only embeddings\n"
                  f"which stays inside the sqlite store and needs no lock.")
            return 2

    rep = Report()
    try:
        # Order matters: relations before supersede (so synonyms of a
        # single-valued relation are seen as the same relation), entities
        # before anchors (so anchors name surviving spellings).
        if "relations" in passes:
            await pass_relations(conn, args.apply, rep)
        if "timestamps" in passes:
            await pass_timestamps(conn, args.apply, rep)
        if "entities" in passes:
            await pass_entities(conn, args.apply, rep)
        if "supersede" in passes:
            await pass_supersede(conn, args.apply, rep)
        if "anchors" in passes:
            await pass_anchors(conn, memory, args.apply, rep)
        if "embeddings" in passes:
            await pass_embeddings(memory, args.apply, rep)
    finally:
        if graph is not None:
            graph.close()

    print(rep.render())
    if not args.apply:
        print("\nDry run: nothing written. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
