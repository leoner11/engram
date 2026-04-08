# Engram — Orchestrator Playbook

You are an orchestrator. You use Engram to understand the codebase, assemble context, and either edit code yourself or delegate to a worker model.

---

## Two modes of operation

**Solo** — you find context and make changes yourself:
```
find_nodes → query(agent) → edit → verify → save
```

**Orchestrator** — you prepare context for a cheaper worker model:
```
find_nodes → query(worker) → spawn worker with package → verify worker's diff → save
```

Use orchestrator mode when the edit is mechanical (renaming, adding a field, updating callers). Use solo mode when the edit requires architectural judgment.

---

## Step 1: Find seeds

If you don't know the exact node IDs yet:
```
engram_find_nodes("what you want to change")
```
Returns ranked candidates with scores and file locations. Pick the ones that match — pass them as explicit seeds in step 2. This is cheap (~200 tokens). Always do this in an unfamiliar codebase instead of guessing.

---

## Step 2: Get context

```
engram_query(
  prompt="what you're doing",
  seeds=["file::function"],           # from step 1 or already known
  change_hints=["renaming a function"],
  output_mode="agent",                # default — full metadata for you to read
)
```

**Read the confidence line immediately:**
```
## Context Confidence: 73% | Recommendation: VERIFY_GAPS
```
- `PROCEED` (≥80%) — good context, continue
- `VERIFY_GAPS` (55–80%) — check warnings below it, may need bridges
- `SKIP_AND_READ` (<55%) — wrong seeds, re-run with explicit seeds or read files directly

**Seed scores** appear under the Seeds header — if a seed shows `match: top_connected_fallback` it means Engram guessed. Treat that context with suspicion.

---

## Step 2b: Fix gaps with bridges

If the query missed frontend components, templates, or API consumers:
```
engram_suggest_bridges()                  → see what's detected
engram_suggest_bridges(confirmed=[0,1])   → write to engram.yaml + auto-rebuild
engram_suggest_bridges(confirmed="all")   → confirm all HIGH (≥80%) suggestions
```

After confirming, the tool auto-re-queries and shows: `Coverage: 65% → 87%`

Manual bridge (engram.yaml):
```yaml
bridges:
  - name: "list view → template"
    from:
      node: "backend/views.py::list_events"
    to:
      files: ["templates/events/list.html"]
    bidirectional: true
```

---

## Step 3a: Edit yourself (solo mode)

Use the context from step 2. Make changes. Go to step 4.

---

## Step 3b: Spawn a worker (orchestrator mode)

Get a worker-ready context package:
```
engram_query(
  prompt="same prompt",
  seeds=["same confirmed seeds"],
  output_mode="worker",     # strips Engram internals, plain English, 8k budget
)
```

Pass the full output directly as the worker's context. Append task instructions:
```
[worker context from engram_query]

Your task: <specific instruction>
Output: a unified diff only. No explanation.
```

Receive the diff. Go to step 4.

---

## Step 4: Verify

```
engram_verify(diff_text="<unified diff>", prompt="<original task>")
```

- `STRUCTURALLY COMPLETE` — shows what was checked (N functions, M files). Safe to commit.
- `INCOMPLETE` — lists missing nodes with HIGH/MEDIUM/LOW confidence. Fix and re-verify.

Check status at any point:
```
engram_status()   → shows "Last task: verified ✓" or "⚠ unverified"
```

---

## Step 5: Save

```
engram_save(
  title="short factual summary",
  content="what happened and why",
  type="decision|bugfix|discovery|architecture",
  node_ids=["file::function"],   # always pass — you know what you just edited
  topic_key="stable-key",        # optional — same key updates instead of duplicating
)
```

Save when: non-obvious behavior found, architectural decision made, bug root cause worth remembering. Skip for routine changes.

---

## Tools

| Tool | When |
|------|------|
| `engram_find_nodes(prompt, limit)` | Before query in unfamiliar code |
| `engram_query(prompt, seeds, output_mode, change_hints)` | Every task |
| `engram_suggest_bridges(confirmed, min_confidence)` | When context has gaps |
| `engram_verify(diff_text, prompt)` | After every patch |
| `engram_status()` | Check verification state |
| `engram_save(title, content, type, node_ids)` | Persist discoveries |
| `engram_search(query, type, full)` | Find past decisions |
| `engram_build(force)` | After major refactors or manual engram.yaml edits |
| `engram_snapshot(refresh)` | Quick project overview |

---

## Node ID format

`<file_path_from_project_root>::<qualified_name>`

`backend/views.py::list_events` · `frontend/useEvents.ts::useEvents` · `backend/models.py::Event`

---

## What Engram cannot do

- Detect Django Meta patterns (`model = Event`) — declare as bridge
- Cross language without bridges
- Understand runtime behavior (dynamic imports, monkey-patching)
- Tell you what's *missing* from config — structural only, not semantic

---

## When to start using Engram

**Engram is useless during greenfield development.** When you're building from scratch, the codebase changes every session. The index goes stale, the graph has no history, the memory has no observations. The orchestrating model already knows the architecture — it just designed it. Don't use Engram yet.

### The threshold

Start using Engram when the codebase becomes larger than one context window. Specifically when this is true:

> A new agent session can no longer hold the entire relevant codebase in context and still have room to reason and write.

Rough file counts:
- 300-500 files for a typical project
- 100-200 files for deep cross-file dependencies
- 50-100 files for multi-language projects (Django + React + mobile)

**The practical signal:** when an agent starts doing 5+ exploratory file reads at the start of every task just to understand what to touch — that's Engram's job. Switch.

### Development phases

```
Phase 1: Architect + build v1
         No Engram. Full codebase fits in context. Build fast.

Phase 2: First working product shipped
         Run engram_build once. Index exists but don't use it yet.
         Keep building normally if codebase still fits in context.

Phase 3: Codebase grows, agents do 5+ reads per task just for orientation
         Switch to Engram workflow.
         Start saving architectural decisions as observations.
         Add bridges as you discover cross-language connections.

Phase 4: Maintenance — bugs, iterations, refactors
         Full Engram workflow. This is where it pays back everything.
         Memory compounds: every saved observation makes future sessions cheaper.
```

### One exception: start saving memory from day one

The graph retrieval should wait until Phase 3. The memory system (`engram_save`) should start from Phase 1.

Even during early development, when a significant architectural decision is made — "using RS256 not HS256 because X", "chose event-driven over polling because Y" — save it. Costs almost nothing. When a Phase 4 agent needs to understand why the system is shaped the way it is, that memory is the difference between understanding the codebase in one query and spending 30,000 tokens re-deriving decisions that were already made.

**The graph is about code structure. The memory is about intent and decisions. Memory should accumulate from day one even if you don't use the graph until the codebase is large enough to justify it.**
