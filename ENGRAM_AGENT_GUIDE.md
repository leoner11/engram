# Engram: Effective Usage Guide for AI Agents

## The Core Principle

**Search before you explore, explore before you query, query only before you change.**

`engram_search` costs almost nothing and can save the entire investigation if prior work exists. `find_nodes` is fast semantic navigation. `engram_query` is expensive change-context assembly — only worth it when you're about to edit something or need call graph context in a large file.

---

## Phase 1: Orient (before touching any files)

```
1. engram_status()          ← node count, languages, recent sessions
2. engram_snapshot()        ← stack, structure, entry points, patterns
3. engram_search("topic")   ← prior decisions, known bugs, past investigations
```

Run `engram_search` here — before any file reads. If someone already investigated this area, you get the answer in seconds instead of re-deriving it from scratch. On a fresh codebase it returns nothing, but the cost is near zero and the payoff when it hits is high.

---

## Phase 2: Navigate (find the right files)

```
4. engram_find_nodes("specific thing you want to read/change")
```

- Use **precise nouns, not questions**: `"csrf token rotate compare"` not `"how does csrf work"`
- Run 3–5 parallel searches for different facets of the task
- This is the highest-value Engram tool for read-heavy tasks — fast semantic index over all nodes, much faster than grepping thousands of files blind

---

## Phase 3: Read

The right tool depends on file size and what you need:

| Situation | Tool |
|---|---|
| Small file (<300 lines) | Read the whole file directly |
| Large file, know the exact lines | Read(offset=N, limit=50) |
| Large file, need the function + its callers/callees | engram_query (seeds explicit, hint: "reading") |
| Audit across many files | find_nodes → Read in parallel |

**Why `engram_query` earns its place on large files:** Files like a 1800-line `compiler.py` or 2000-line `query.py` waste context when read whole. `engram_query` with explicit seeds pulls out just the functions connected to your task — full source for seeds, excerpts for callers, signatures for distant deps. You get the shape of the area without loading 1800 lines.

```
# For read-only comprehension in large files:
engram_query(
  prompt="trace X flow",          ← "trace", "read", not "find bugs"
  seeds=[confirmed node IDs],
  change_hints=["reading"],       ← broad traversal, don't prune
)
→ if confidence < 55%: fall back to Read with offset
```

The prompt and hints still matter here. `"reading csrf token flow"` gets broader context than `"find security bugs in csrf"`. The tool needs to know it's traversing for comprehension, not for change propagation.

---

## Phase 4: Change (if making edits)

```
5. engram_query(
     prompt="verb + what you're doing",    ← "rename X", "add field to Y"
     seeds=[confirmed node IDs],            ← always explicit, never auto
     change_hints=["modifying signature"],  ← operation type, not concern
   )
6. Make the edit
7. engram_verify(diff)
```

Only enter this path if actually editing code. The query prompt should imply a change type so activation rules fire correctly:

| Prompt style | Why it works |
|---|---|
| "rename `SeedSelector.select`" | Triggers RENAME — traverses all references |
| "add field to `Order` model" | Triggers FIELD_ADDITION — exhaustive type users + subclasses |
| "change `process_order` signature" | Triggers SIGNATURE_MODIFICATION — CALLS + USES_TYPE + IMPORTS + EXTENDS |
| "modify body of `validate_user_id`" | Triggers BODY_MODIFICATION — direct callers only |

**Budget strategy:** Multiple narrow 4–8K queries beat one broad query. Seed quality matters more than budget size — a perfect 4K result beats a scattered 16K result.

---

## Phase 5: Save (end of session)

```
8. engram_save() for anything non-obvious:
   - Architectural decisions made
   - Bugs found and why they exist
   - Security issues (even ones not fixed yet)
   - "Decided X over Y because Z"
```

- **Skip** routine changes and anything derivable from the code itself
- **Title must be keyword-rich** — search matches titles more than body
- These observations link to graph nodes and surface in future `engram_search` results

---

## Quick Reference

| Task | Tools | Skip |
|---|---|---|
| Start of any session | status → snapshot → search | — |
| Find relevant files | find_nodes (parallel, precise nouns) | — |
| Small file, read-only | find_nodes → Read directly | engram_query |
| Large file, need call graph | find_nodes → query (hint: "reading") | — |
| Making a change | find_nodes → query (explicit seeds) → edit → verify | — |
| End of session | save (non-obvious findings only) | routine changes |

---

## Why This Order Matters

The feedback loop is built into the workflow:

- **`engram_verify`** records which nodes were missed — `FeedbackBooster` uses this to boost those nodes in future traversals
- **`engram_save`** persists decisions that surface via `engram_search` in future sessions
- **`engram_search` at the start** is what closes the loop — it catches prior work before you redo it

Skipping verify or save means the system never learns. Skipping search at the start means you never benefit from what it learned.

---

## What Engram Is Not Good At

- **Small codebases** — If the whole thing fits in context, just read it directly
- **Exploration without a task** — Use `engram_snapshot` for overviews, not `engram_query`
- **Languages beyond Python, TypeScript, JavaScript, Dart** — No tree-sitter adapters yet for Go, Rust, Java, C++
- **Vague prompts without seeds** — Falls back to most-connected nodes, which is basically random. Always provide explicit seeds from `find_nodes`
