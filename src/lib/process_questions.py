"""Question bank for guided process mapping.

Pure data: every entry ties a question to the *field* of the process model it
fills. `src/tools/process_map.py` walks the model, finds empty / hedged /
contradictory fields and picks the matching questions — so the agent asks
about what is actually missing instead of reciting a generic checklist.

Sources (synthesised): SIPOC, RACI/swimlanes, Value Stream Mapping
(Rother & Shook; Karen Martin), BPMN discovery (Camunda "beyond the happy
path"), Lean 8 wastes, 5 Whys, Theory of Constraints, service blueprinting,
event storming, JTBD, Wardley's "Finding a path" + evolution cheat sheet and
Ben Mosior's Wardley Mapping Canvas.

Entry fields:
  phase      ordering bucket (see PHASES / WARDLEY_PHASES)
  field      dotted path in the model this question fills ("" = meta)
  question   what to ask — one question, plain language
  why        why it matters (shown to the user so the question feels earned)
  method     the framework it comes from
  lens       optional method lens tag (sipoc, raci, vsm, bpmn, wastes, ...)
  impact     1-3 — how much a blank here hurts the map (ranking weight)
"""

from __future__ import annotations

from typing import TypedDict


class Question(TypedDict):
    phase: str
    field: str
    question: str
    why: str
    method: str
    lens: str
    impact: int


PHASES: list[str] = [
    "scope", "trigger", "actors", "flow", "decisions",
    "data", "metrics", "pain", "future",
]

WARDLEY_PHASES: list[str] = [
    "purpose", "users", "needs", "chain", "evolution", "movement", "climate", "doctrine", "validate",
]

LENSES: list[str] = ["sipoc", "raci", "vsm", "bpmn", "wastes", "wardley", "blueprint", "events"]

# Hedge words that mean "there is a rule here nobody has stated yet".
HEDGES: tuple[str, ...] = (
    "usually", "sometimes", "it depends", "depends", "normally", "typically",
    "often", "mostly", "in general", "as needed", "if necessary", "etc",
    "somebody", "someone", "whoever", "tbd", "?",
)


def _q(phase: str, field: str, question: str, why: str, method: str,
       lens: str = "", impact: int = 2) -> Question:
    return {"phase": phase, "field": field, "question": question, "why": why,
            "method": method, "lens": lens, "impact": impact}


# ---------------------------------------------------------------------------
# Process bank (flowchart / swimlane / sequence models)
# ---------------------------------------------------------------------------

PROCESS_QUESTIONS: list[Question] = [
    # --- 0. scope & purpose ---
    _q("scope", "title", "What is this process called, and what does it produce when it works?",
       "A name and an output are the anchor every other question hangs on.", "SIPOC", "sipoc", 3),
    _q("scope", "purpose", "Why are we mapping this now — what decision or problem hangs on it, and who will use the map?",
       "The purpose decides how deep to go and which view (flow, handoffs, timing) matters.", "Gause & Weinberg", "", 3),
    _q("scope", "scope.start", "Where exactly does the process start — what is the very first thing that happens?",
       "Boundaries stop the map from sprawling into neighbouring processes.", "SIPOC", "sipoc", 3),
    _q("scope", "scope.end", "Where does it end — what is the last thing that happens, and what leaves the process?",
       "An explicit end point defines the deliverable and the customer of the process.", "SIPOC", "sipoc", 3),
    _q("scope", "scope.out_of_scope", "What is explicitly out of scope for this map?",
       "Naming what is excluded prevents later 'but what about…' drift.", "Wardley Canvas", "", 1),
    _q("scope", "", "Are you the right person to answer these, and is there anyone else I should talk to or a place I can see it happening?",
       "Second sources catch the gap between the official process and what actually happens.", "Gause & Weinberg", "", 1),

    # --- 1. trigger & end states ---
    _q("trigger", "trigger", "What makes you realise the process needs to start today — a request, a timer, a message, a system event? Is there more than one way it starts?",
       "Processes usually have several start events; missing one means a whole path is missing.", "BPMN / Camunda", "bpmn", 3),
    _q("trigger", "end_states", "What is the desired end result — and what undesired end results can it reach (rejected, abandoned, timed out)?",
       "Exceptions are designed backwards from the undesired end states.", "Camunda 'beyond the happy path'", "bpmn", 3),
    _q("trigger", "end_states", "How do you know it is finished, and who is told?",
       "The finish signal is often an unmapped handoff to the next process.", "BPMN", "bpmn", 2),

    # --- 2. actors & handoffs ---
    _q("actors", "actors", "Who initiates it, who does the work, who approves, who must be consulted before, and who is informed after?",
       "RACI per step exposes missing owners and double approvals.", "RACI", "raci", 3),
    _q("actors", "actors.approver", "Who has the final say (exactly one person or role) on this process?",
       "Two accountable parties means nobody is; zero means decisions stall.", "RACI", "raci", 2),
    _q("actors", "actors", "Who else touches this beyond the people we have named — including systems, suppliers or customers?",
       "Event storming: ask 'who cares about this event?' at every step.", "Event storming", "events", 2),
    _q("actors", "steps.handoff", "Where does work cross from one person or team to another — what is passed, in what form, and how does the receiver know it has arrived?",
       "Handoffs are where work waits, gets lost, or is re-keyed.", "Swimlanes (Rummler-Brache)", "raci", 3),
    _q("actors", "actors.system", "Which of these steps are done by a system rather than a person?",
       "Separating system lanes makes automation candidates and integration gaps visible.", "Service blueprint", "blueprint", 2),

    # --- 3. flow (happy path) ---
    _q("flow", "steps", "Walk me through the last time this ran on a typical day — what happened first? Then what? Then what?",
       "Narrating a concrete instance beats describing the ideal procedure.", "Story mapping / JTBD", "", 3),
    _q("flow", "steps", "Before the core step: what must be defined, gathered, prepared and confirmed? After it: what is monitored, adjusted, concluded?",
       "Ulwick's universal job map — the eight steps every job has; it catches forgotten prep and wrap-up.", "JTBD job map", "", 2),
    _q("flow", "edges", "Which steps could happen in parallel, and which are done in batches — how big and how often?",
       "Batching and false sequencing are the biggest hidden lead-time drivers.", "VSM", "vsm", 2),
    _q("flow", "edges", "Reverse narrative: for the last step to happen, what had to be true just before it? And before that?",
       "Walking backwards finds 30-40% of missing events in workshops.", "Event storming", "events", 2),

    # --- 4. decisions, rules & exceptions ---
    _q("decisions", "steps.decision", "At this decision, who decides and by what rule? Is the rule written down? Is it based on data you have now, or on waiting for something?",
       "Data-based vs event-based decisions are drawn differently and fail differently.", "BPMN gateways", "bpmn", 3),
    _q("decisions", "edges.label", "What are the possible outcomes at this decision (approve / changes / reject…), and what happens on each?",
       "A decision with one unlabeled arrow is a guess, not a decision.", "BPMN", "bpmn", 3),
    _q("decisions", "exceptions", "Tell me about the last time this went wrong. What was the hardest version you've dealt with?",
       "Stories surface real exceptions; abstract 'what could go wrong' rarely does.", "Process intake practice", "", 3),
    _q("decisions", "exceptions", "What happens when an input fails, is late, incomplete or never arrives? How long do you wait before giving up, and who is escalated to?",
       "Timeouts and escalations are the exception paths most maps omit.", "Camunda", "bpmn", 2),
    _q("decisions", "exceptions", "Are there workarounds people use that are not in the official process?",
       "Workarounds mark where the designed process does not fit reality.", "As-is analysis", "", 2),
    _q("decisions", "", "What else could happen that we haven't discussed? Would anyone ever want to…? Could … ever occur?",
       "Wiegers' probes for the unstated alternative paths.", "Wiegers", "", 1),

    # --- 5. data, artifacts & systems ---
    _q("data", "steps.inputs", "What document or data do you need before you can begin each step, and where does it come from?",
       "Inputs name the suppliers and the upstream process.", "SIPOC / Turtle", "sipoc", 2),
    _q("data", "steps.outputs", "What does the person receiving your output actually do with it?",
       "Outputs nobody uses are waste; outputs used differently than intended are defects.", "SIPOC", "sipoc", 2),
    _q("data", "steps.systems", "Which systems do you open, update or check during this step? Where does the data live? Is anything re-keyed between systems?",
       "Re-keying marks integration gaps and error sources.", "Turtle 'with what'", "sipoc", 3),
    _q("data", "steps.inputs", "What information does the decision-maker need in front of them to decide?",
       "Event storming read models — missing info is why decisions wait.", "Event storming", "events", 2),

    # --- 6. metrics & volume ---
    _q("metrics", "steps.metrics.frequency", "How often does this run (per day / week / month), and how many cases are in flight right now?",
       "Volume turns a pretty map into a capacity conversation.", "VSM", "vsm", 2),
    _q("metrics", "steps.metrics.process_time", "For each step: how long does the actual work take (process time), and how long from becoming available to being done (lead time)? Where does it wait, and why?",
       "PT vs LT is the single most revealing metric pair in a value stream.", "VSM (Karen Martin)", "vsm", 3),
    _q("metrics", "steps.metrics.pct_complete_accurate", "What share of incoming work is complete and accurate on arrival, and how often is work sent back or redone?",
       "%C&A exposes rework loops that never appear on the happy path.", "VSM (Karen Martin)", "vsm", 2),
    _q("metrics", "", "Where is the bottleneck — which step or person is overloaded, and what is the customer demand (takt)?",
       "Theory of Constraints: only the constraint limits throughput.", "TOC / VSM", "vsm", 2),
    _q("metrics", "", "How is performance measured today, by whom, how often — and what SLA or targets exist?",
       "Turtle 'how many' — if nobody measures it, nobody owns it.", "Turtle diagram", "sipoc", 2),

    # --- 7. pain points, waste & root cause ---
    _q("pain", "pain_points", "What is the most frustrating part of this process for you? Where does the customer feel it worst?",
       "Gemba opener — the answer usually points straight at a waste.", "Lean gemba", "wastes", 3),
    _q("pain", "pain_points", "Where do people wait? Where are approvals stacked? What reports does nobody read? Where do skilled people do menial work?",
       "The eight wastes, asked as observations.", "Lean 8 wastes", "wastes", 2),
    _q("pain", "pain_points", "Why does that happen? (and why does *that* happen — until the cause is systemic, not a person)",
       "5 Whys — stop at a cause the system can fix.", "5 Whys", "wastes", 2),
    _q("pain", "pain_points", "What undesirable effects do you see, and what conflict forces you to live with them?",
       "TOC thinking processes: the core conflict behind recurring pain.", "Theory of Constraints", "wastes", 1),

    # --- 8. future state ---
    _q("future", "", "Where do we want to be — what measurable target condition, and what does the customer truly value?",
       "Future state needs a target, not just 'better'.", "As-is / to-be", "vsm", 2),
    _q("future", "", "Which steps add value, which are necessary non-value, and which are waste? Which handoffs or approvals can be eliminated or combined?",
       "Karen Martin's future-state questions.", "VSM", "vsm", 2),
    _q("future", "", "What would you change first if you could — and what has stopped that so far (habit, anxiety, policy, system)?",
       "JTBD forces: push/pull vs habit/anxiety explain why nothing changed.", "JTBD four forces", "", 1),
    _q("future", "", "What policies or metrics need to change to enable it, who owns each change, and how will we measure success?",
       "Ownership and measures turn a to-be map into a plan.", "VSM / TOC", "vsm", 1),
]


# ---------------------------------------------------------------------------
# Wardley bank
# ---------------------------------------------------------------------------

WARDLEY_QUESTIONS: list[Question] = [
    _q("purpose", "purpose", "What are you trying to see with this map? What must succeed but has many moving parts?",
       "Purpose sets scope; 'don't map the world'.", "Wardley Canvas (Mosior)", "wardley", 3),
    _q("purpose", "title", "What is included in this map, and what is explicitly excluded?",
       "Scope keeps the component count under ~20 for a first map.", "Wardley Canvas", "wardley", 2),
    _q("users", "anchors", "Who is the user — who is expecting something from you or asking for help? Who is missing from the picture?",
       "Start with the user, not the org chart; if you start with the org chart you map responsibilities, not dependencies.", "Wardley ch.2", "wardley", 3),
    _q("needs", "anchors", "What does the user need to reach their goal — not what *you* need (your profit is not their need)?",
       "The anchor is the user need; everything else hangs from it.", "Wardley ch.2", "wardley", 3),
    _q("needs", "components", "What is their first interaction with you, what happens next, and what happens last? Are there unmet or novel needs?",
       "The user journey surfaces needs you did not list.", "Wardley Canvas", "wardley", 2),
    _q("chain", "links", "For each component: what components do we need in order to build or provide it? (recurse until you hit something you buy or rent)",
       "The value chain is built by asking 'what does this need?' until the external boundary.", "Wardley ch.2", "wardley", 3),
    _q("chain", "components", "Which components are visible to the user and which are invisible plumbing? Is an important dependency missing or unmanaged?",
       "Visibility is the vertical axis; invisible-but-critical components are where risk hides.", "Wardley Canvas", "wardley", 2),
    _q("chain", "components.type", "Is each component an activity, a practice, a piece of data, or knowledge?",
       "Different kinds evolve with different labels (e.g. novel → emerging → good → best practice).", "Wardley", "wardley", 1),
    _q("evolution", "components.evolution", "How ubiquitous and well defined is this component? Do all your competitors use it? Is it available as a product or utility? Is it new?",
       "Wardley's placement questions; use the cheat sheet, not the marketing.", "Wardley ch.2 + cheat sheet", "wardley", 3),
    _q("evolution", "components.stage_rationale", "What do publications about this component look like — wonder, how-to-build, feature comparison, or guides to use? Is failure tolerated or a surprise?",
       "Weak signals from the evolution cheat sheet make placement defensible.", "Evolution cheat sheet", "wardley", 2),
    _q("evolution", "components.stage_rationale", "Are you marketing this as new or bespoke when the rest of the world treats it as a product or commodity?",
       "Map evolution, not maturity — custom bias is the most common placement error.", "Wardley", "wardley", 2),
    _q("evolution", "components.build_buy_outsource", "Are there Commodity components you haven't outsourced, or Product components you haven't bought off the shelf? What are you building right now that is like Thomas Thwaites's toaster?",
       "Doctrine: use appropriate methods — build genesis, buy product, rent commodity.", "Wardley Canvas", "wardley", 3),
    _q("movement", "components.evolve_to", "Which components are moving — product → utility, custom → product? What is the evidence, and how fast?",
       "Movement is what the map is for; a static map is a diagram.", "Wardley", "wardley", 2),
    _q("movement", "components.inertia", "Where does past success create inertia — customers, suppliers, culture, regulation, sunk cost? Who are the new entrants not encumbered by it?",
       "Past success breeds inertia; new entrants initiate change.", "Wardley climatic patterns", "wardley", 2),
    _q("climate", "climatic_patterns", "Which climatic patterns apply here — everything evolves, efficiency enables innovation, higher-order systems create new value, co-evolution of practice, competitors' actions? What does each imply for a component?",
       "Climate = the rules of the game that act on the map regardless of your choices.", "Wardley ch.3", "wardley", 1),
    _q("doctrine", "", "Where do you duplicate effort, custom-build standards, lack a common language, or fail to challenge assumptions?",
       "Phase I doctrine ('stop self-harm') — cheap wins visible on any map.", "Wardley doctrine", "wardley", 1),
    _q("validate", "", "Can someone unfamiliar retell the story from this map? Is it under ~20 components? Who will challenge it?",
       "Challenge the map, not the individual; an imperfect but useful map is success.", "Mosior / IT Revolution", "wardley", 1),
]


def bank_for(kind: str) -> list[Question]:
    return WARDLEY_QUESTIONS if kind == "wardley" else PROCESS_QUESTIONS


def phases_for(kind: str) -> list[str]:
    return WARDLEY_PHASES if kind == "wardley" else PHASES
