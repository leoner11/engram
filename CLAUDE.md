# THIS IS TO BE PUT IN THE MAIN DIRECTORY OF THE PROJECT, WHERE THE MAIN AGENT (LEADER / ORCHESTRATOR) LIVES AND SEE AND HE/SHE/IT CAN DECIDE ON WHETHER HE WANTS TO DO IT ALONE OR NO

# Engram — Orchestrator Playbook

You are an orchestrator. You use Engram to understand the codebase, assemble context, and either edit code yourself or delegate to a worker model.

**Core principle: search before you explore, explore before you query, query only before you change.**

---

## Two modes of operation

**Solo** — you find context and make changes yourself:
```
orient → find_nodes → read or query → edit → verify → save
```

**Orchestrator** — you prepare context for a cheaper worker model:
```
orient → find_nodes → query(worker) → spawn worker with package → verify worker's diff → save
```

Use orchestrator mode when the edit is mechanical (renaming, adding a field, updating callers). Use solo mode when the edit requires architectural judgment.

---

## Phase 1: Orient (before touching any files)

```
engram_status()          ← node count, languages, recent sessions
engram_snapshot()        ← stack, structure, entry points, patterns
engram_search("topic")   ← prior decisions, known bugs, past investigations
```

Run `engram_search` before any file reads. If someone already investigated this area, you get the answer in seconds instead of re-deriving it from scratch. On a fresh codebase it returns nothing, but the cost is near zero and the payoff when it hits is high.

---

## Phase 2: Find seeds

```
engram_find_nodes("what you want to read/change")
```

- Use **precise nouns, not questions**: `"csrf token rotate compare"` not `"how does csrf work"`
- Run 3–5 parallel searches for different facets of the task
- Returns ranked candidates with scores and file locations
- Pick the ones that match — pass them as explicit seeds later
- This is cheap (~200 tokens). Always do this instead of guessing

---

## Phase 3: Read

The right tool depends on file size and what you need:

| Situation | Tool |
|---|---|
| Small file (<300 lines) | Read the whole file directly |
| Large file, know the exact lines | Read(offset=N, limit=50) |
| Large file, need the function + its callers/callees | engram_query (seeds explicit, hint: "reading") |
| Audit across many files | find_nodes → Read in parallel |

**Skip `engram_query` for simple reads.** It traverses the graph based on change-propagation rules — useless when you just need to see a small file.

**Use `engram_query` for large files** where reading the whole thing wastes context. With explicit seeds it pulls out just the connected functions — full source for seeds, excerpts for callers, signatures for distant deps.

```
# For read-only comprehension in large files:
engram_query(
  prompt="trace X flow",          ← "trace", "read", not "find bugs"
  seeds=["file::function"],
  change_hints=["reading"],       ← broad traversal, don't prune
)
→ if confidence < 55%: fall back to Read with offset
```

---

## Phase 4: Change (if making edits)

```
engram_query(
  prompt="what you're doing",
  seeds=["file::function"],           # from find_nodes or already known
  change_hints=["renaming a function"],
  output_mode="agent",                # default — full metadata for you to read
)
```

The prompt should imply a change type so activation rules fire correctly:

| Prompt style | What it triggers |
|---|---|
| "rename `SeedSelector.select`" | RENAME — traverses all references |
| "add field to `Order` model" | FIELD_ADDITION — exhaustive type users + subclasses |
| "change `process_order` signature" | SIGNATURE_MODIFICATION — CALLS + USES_TYPE + IMPORTS + EXTENDS |
| "modify body of `validate_user_id`" | BODY_MODIFICATION — direct callers only |

**Budget strategy:** Multiple narrow 4–8K queries beat one broad query. Seed quality matters more than budget size.

**Read the confidence line immediately:**
```
## Context Confidence: 73% | Recommendation: VERIFY_GAPS
```
- `PROCEED` (≥80%) — good context, continue
- `VERIFY_GAPS` (55–80%) — check warnings below it, may need bridges
- `SKIP_AND_READ` (<55%) — wrong seeds, re-run with explicit seeds or read files directly

If a seed shows `match: top_connected_fallback` it means Engram guessed. Treat that context with suspicion.

### Fix gaps with bridges

If the query missed frontend components, templates, or API consumers:
```
engram_suggest_bridges()                  → see what's detected
engram_suggest_bridges(confirmed=[0,1])   → write to engram.yaml + auto-rebuild
engram_suggest_bridges(confirmed="all")   → confirm all HIGH (≥80%) suggestions
```

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

### Spawning a worker (orchestrator mode)

```
engram_query(
  prompt="same prompt",
  seeds=["same confirmed seeds"],
  output_mode="worker",     # strips Engram internals, plain English, 8k budget
)
```

Pass the full output directly as the worker's context:
```
[worker context from engram_query]

Your task: <specific instruction>
Output: a unified diff only. No explanation.
```

---

## Phase 5: Verify

```
engram_verify(diff_text="<unified diff>", prompt="<original task>")
```

- `STRUCTURALLY COMPLETE` — shows what was checked (N functions, M files). Safe to commit.
- `INCOMPLETE` — lists missing nodes with HIGH/MEDIUM/LOW confidence. Fix and re-verify.

**Don't skip this.** It records which nodes were missed, and `FeedbackBooster` uses that data to boost those nodes in future traversals. Skipping verify means the system never learns.

Check status at any point:
```
engram_status()   → shows "Last task: verified ✓" or "⚠ unverified"
```

---

## Phase 6: Save

```
engram_save(
  title="short factual summary",
  content="what happened and why",
  type="decision|bugfix|discovery|architecture",
  node_ids=["file::function"],   # always pass — you know what you just edited
  topic_key="stable-key",        # optional — same key updates instead of duplicating
)
```

Save when:
- Non-obvious behavior found
- Architectural decision made
- Bug root cause worth remembering
- Security issues (even ones not fixed yet)
- "Decided X over Y because Z"

**Skip** routine changes and anything derivable from the code itself.

**Title must be keyword-rich** — search matches titles more than body. These observations link to graph nodes and surface in future `engram_search` results, which is what closes the feedback loop.

---

## Quick reference

| Task | Tools | Skip |
|---|---|---|
| Start of any session | status → snapshot → search | — |
| Find relevant files | find_nodes (parallel, precise nouns) | — |
| Small file, read-only | find_nodes → Read directly | engram_query |
| Large file, need call graph | find_nodes → query (hint: "reading") | — |
| Making a change | find_nodes → query (explicit seeds) → edit → verify | — |
| End of session | save (non-obvious findings only) | routine changes |

---

## Why this workflow order matters

Read **[ENGRAM_AGENT_GUIDE.md](./ENGRAM_AGENT_GUIDE.md)** for the reasoning behind each phase — why search comes before explore, why `engram_query` should be skipped for read-only tasks, how the feedback loop between verify/save/search compounds over sessions, and common mistakes that waste tokens.

---

## Tools

| Tool | When |
|------|------|
| `engram_status()` | Start of session, check verification state |
| `engram_snapshot(refresh)` | Start of session, quick project overview |
| `engram_search(query, type, full)` | Start of session, find past decisions |
| `engram_find_nodes(prompt, limit)` | Before any read or query — get exact node IDs |
| `engram_query(prompt, seeds, output_mode, change_hints)` | Large file reads, change planning |
| `engram_suggest_bridges(confirmed, min_confidence)` | When context has cross-language gaps |
| `engram_verify(diff_text, prompt)` | After every patch |
| `engram_save(title, content, type, node_ids)` | End of session, persist discoveries |
| `engram_build(force)` | After major refactors or manual engram.yaml edits |

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
         Exception: start using engram_save for architectural decisions from day one.

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