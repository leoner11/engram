# Engram

**Persistent memory and context retrieval for AI coding agents.**

Engram is built exclusively for agents — not developers. It solves the problem every agent has: starting each session cold, with no memory of what was built before and no understanding of how the codebase connects. Engram gives agents the right context for any task without reading every file, and remembers decisions across sessions so future agents don't re-derive what previous agents already figured out.

---

## What it does

**Indexes your codebase** into a dependency graph (functions, classes, types, relationships) using tree-sitter. Supports Python, TypeScript, JavaScript, Dart.

**Retrieves task-adaptive context** — given "fix the webhook renewal bug", returns only the structurally relevant code at appropriate detail levels within a token budget. A rename needs different context than a body fix. Engram knows the difference.

**Suggests and manages bridges** — detects implicit cross-language connections (Django views → templates, TypeScript fetch → Python API) and suggests them for confirmation. Declared bridges become real graph edges traversed automatically.

**Verifies patches structurally** — after an agent generates a diff, checks whether all affected nodes were touched. Catches missed callers and broken interfaces before tests run.

**Persists memory across sessions** — observations (decisions, bugfixes, discoveries) survive session boundaries and link to graph nodes. Future agents inherit context from past agents.

**Supports orchestrator → worker pipelines** — `output_mode="worker"` produces clean context packages with no Engram internals, ready to pass directly to cheaper models (Haiku, Gemini, etc.) as their task context.

---

## When to use it

**Not during greenfield development.** While you're building from scratch, the codebase changes every session and the orchestrating agent already knows the architecture it just designed. Don't index yet.

**Start when** an agent consistently needs 5+ file reads at the start of a task just to understand what to touch. That's the signal. Roughly:
- 100-200 files for projects with deep cross-file dependencies
- 300-500 files for typical projects
- 50-100 files for multi-language projects (backend + frontend + mobile)

**Start saving memory from day one** even if you're not using the graph yet. Architectural decisions saved during early development become the primary context source for maintenance agents months later.

---

## MCP setup

Add to `.claude/settings.json` (or equivalent for your agent framework):

```json
{
  "mcpServers": {
    "engram": {
      "command": "python",
      "args": ["-m", "engram.mcp"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

Then index your project once:

```bash
pip install -e "."
engram build --root /path/to/your/project
```

---

## MCP tools

| Tool | Purpose |
|------|---------|
| `engram_find_nodes(prompt)` | Find candidate seed node IDs before querying — breaks the chicken-and-egg of needing to know node IDs upfront |
| `engram_query(prompt, seeds, output_mode)` | Get task-adaptive context. `output_mode="worker"` for clean packages to pass to cheaper models |
| `engram_suggest_bridges(confirmed)` | Auto-detect cross-language gaps and write bridges to engram.yaml |
| `engram_verify(diff_text)` | Structural completeness check after generating a patch |
| `engram_save(title, content, type, node_ids)` | Persist observations across sessions |
| `engram_search(query)` | Full-text search across saved observations |
| `engram_status()` | Project overview + last task verification state |
| `engram_build(force)` | Rebuild index after major refactors |

See `CLAUDE.md` for the full agent workflow and `CLAUDE_WORKER.md` for worker model instructions.

---

## Orchestrator → worker pattern

```
Orchestrator (Sonnet/Opus):
  engram_find_nodes("fix the pagination bug")     → pick seed IDs cheaply
  engram_query(seeds=[...], output_mode="worker") → clean 8k context package
  → spawn Haiku/Gemini with that package
  → receive diff back
  engram_verify(diff_text=diff)                   → quality gate
  engram_status()                                 → verified ✓ or ⚠ unverified
```

Cost reduction vs naive agent reading files: **60-70% token reduction**, **65-70% cost reduction** when shifting execution to cheaper models.

---

## Token savings

| Mode | Context cost without Engram | With Engram | Saving |
|------|----------------------------|-------------|--------|
| Solo agent, unfamiliar codebase | 25,000-45,000 tokens | 4,000-6,000 tokens | ~75-85% |
| Orchestrator → worker | 35,000 Sonnet tokens | 8,000 Sonnet + 10,000 Haiku | ~68% cost |
| Verify preventing retry loops | 20,000-30,000 per retry | 500 tokens | ~98% |

Realistic combined saving in production: **60-70%** accounting for occasional bad queries and overhead.

---

## Change type system

Different changes activate different subsets of the graph:

| Change type | What gets pulled |
|-------------|-----------------|
| `BODY_MODIFICATION` | Direct callers only |
| `SIGNATURE_MODIFICATION` | All callers + type users + imports + subclasses |
| `RENAME` | All references everywhere |
| `FIELD_ADDITION` | Exhaustive type users + subclasses |
| `FIELD_REMOVAL` | Users accessing the removed field |
| `DELETION` | All references everywhere |

Pass `change_hints=["renaming a function"]` to `engram_query` to activate the right traversal.

---

## Install

```bash
pip install -e ".[dev]"        # with dev tools
pip install -e ".[precise]"    # with tiktoken for accurate token counting
pip install -e ".[dart]"       # with Dart tree-sitter support
```

---

## License

MIT
