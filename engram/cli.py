"""Engram CLI: build, query, status, export."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.indexer.extractor import PythonExtractor
from engram.indexer.hasher import hash_file
from engram.indexer.languages import detect_language, get_extensions, get_all_extensions
from engram.indexer.parser import TreeSitterParser
from engram.indexer.resolver import Resolver
from engram.indexer.scanner import scan_project
from engram.indexer.adapters.python_adapter import PythonAdapter
from engram.retriever.assembler import ContextAssembler


def _get_adapter(language: str):
    """Get the appropriate language adapter."""
    if language == "python":
        return PythonAdapter()
    elif language == "typescript":
        from engram.indexer.adapters.typescript_adapter import TypeScriptAdapter
        return TypeScriptAdapter()
    elif language == "javascript":
        from engram.indexer.adapters.javascript_adapter import JavaScriptAdapter
        return JavaScriptAdapter()
    elif language == "dart":
        from engram.indexer.adapters.dart_adapter import DartAdapter
        return DartAdapter()
    return None


def build_index(root: Path, db: EngramDB, force: bool = False) -> dict:
    """Build or update the code index. Returns stats dict."""
    store = GraphStore(db)

    if force:
        db.reset()
        store = GraphStore(db)

    parser = TreeSitterParser()

    # Scan for ALL supported source files
    extensions = get_all_extensions()
    files = scan_project(root, extensions)

    if not files:
        return {"files_scanned": 0, "files_changed": 0, "nodes": 0, "edges": 0, "time": 0}

    start = time.time()
    files_changed = 0
    all_nodes = {}
    all_raw_edges = []

    # Phase 1: Parse and extract
    for rel_path in files:
        abs_path = root / rel_path
        file_str = str(rel_path)
        language = detect_language(rel_path)
        if language is None:
            continue

        current_hash = hash_file(abs_path)
        stored_hash = store.get_manifest_hash(file_str)

        if stored_hash == current_hash and not force:
            # Unchanged — load existing nodes for resolver
            for node in store.get_nodes_by_file(file_str):
                all_nodes[node.id] = node
            continue

        files_changed += 1

        # Delete old data for this file
        store.delete_edges_by_file(file_str)
        store.delete_nodes_by_file(file_str)
        store.delete_manifest(file_str)

        # Parse
        try:
            tree = parser.parse_file(abs_path, language)
        except Exception as e:
            click.echo(f"  ⚠ Parse error: {rel_path}: {e}", err=True)
            continue

        source = abs_path.read_bytes()

        # Extract using language adapter
        adapter = _get_adapter(language)
        if adapter is None:
            continue

        try:
            nodes, raw_edges = adapter.extract(tree, source, file_str)
        except Exception as e:
            click.echo(f"  ⚠ Extract error: {rel_path}: {e}", err=True)
            continue

        # Store nodes
        for node in nodes:
            store.upsert_node(node)
            all_nodes[node.id] = node

        all_raw_edges.extend(raw_edges)

        # Update manifest
        store.update_manifest(file_str, current_hash, len(nodes))

    # Phase 2: Resolve edges (needs all nodes available)
    if files_changed > 0 or force:
        # If incremental, we need existing nodes + edges too
        if not force:
            existing_nodes = store.get_all_nodes()
            for nid, node in existing_nodes.items():
                if nid not in all_nodes:
                    all_nodes[nid] = node

        resolver = Resolver(all_nodes, all_raw_edges, root)
        resolved_edges = resolver.resolve_all()

        # Store resolved edges
        for edge in resolved_edges:
            store.upsert_edge(edge)

    store.commit()

    # Populate FTS5 seed selection index
    from engram.retriever.seeds import populate_node_index
    populate_node_index(store)

    # Build cross-language bridge edges from engram.yaml
    from engram.bridges import build_bridges
    bridge_count = build_bridges(root, store)

    elapsed = time.time() - start

    stats = store.get_stats()
    stats["files_scanned"] = len(files)
    stats["files_changed"] = files_changed
    stats["bridge_edges"] = bridge_count
    stats["time"] = round(elapsed, 2)

    return stats


@click.group()
def main():
    """Engram: External cognitive system for LLMs."""


@main.command()
@click.option("--root", type=click.Path(exists=True), default=".", help="Project root directory")
@click.option("--force", is_flag=True, help="Force full rebuild")
def build(root, force):
    """Build or update the code index."""
    root_path = Path(root).resolve()
    click.echo(f"Indexing {root_path} ...")

    db = EngramDB(root_path)
    stats = build_index(root_path, db, force=force)

    click.echo(f"Done in {stats['time']}s")
    click.echo(f"  Files: {stats['files_scanned']} scanned, {stats['files_changed']} changed")
    click.echo(f"  Nodes: {stats.get('node_count', 0)} ({stats.get('function_count', 0)} functions, {stats.get('class_count', 0)} classes)")
    click.echo(f"  Edges: {stats.get('edge_count', 0)}")
    if stats.get('bridge_edges', 0) > 0:
        click.echo(f"  Bridges: {stats['bridge_edges']} cross-language edges from engram.yaml")

    db.close()


@main.command()
@click.option("--root", type=click.Path(exists=True), default=".", help="Project root directory")
def bridges(root):
    """Show cross-language bridge declarations and their resolved edges."""
    root_path = Path(root).resolve()

    from engram.bridges import load_config, parse_bridges, resolve_bridge_edges

    config = load_config(root_path)
    if config is None:
        click.echo("No engram.yaml found. Create one to declare cross-language bridges.")
        click.echo("\nExample engram.yaml:")
        click.echo("  bridges:")
        click.echo("    - name: \"Event API\"")
        click.echo("      backend:")
        click.echo("        node: \"backend/views.py::list_events\"")
        click.echo("      frontend:")
        click.echo("        files: [\"frontend/useEvents.ts\"]")
        click.echo("      bidirectional: true")
        return

    bridge_decls = parse_bridges(config)
    if not bridge_decls:
        click.echo("engram.yaml found but no bridges declared.")
        return

    db = EngramDB(root_path)
    store = GraphStore(db)

    click.echo(f"Found {len(bridge_decls)} bridge(s) in engram.yaml:\n")
    for bridge in bridge_decls:
        click.echo(f"  {bridge.name}")
        click.echo(f"    Edge kind: {bridge.edge_kind}")
        click.echo(f"    Bidirectional: {bridge.bidirectional}")

        edges = resolve_bridge_edges(bridge, store)
        if edges:
            click.echo(f"    Resolved edges: {len(edges)}")
            for edge in edges[:6]:
                click.echo(f"      {edge.source_id} → {edge.target_id}")
            if len(edges) > 6:
                click.echo(f"      ... and {len(edges) - 6} more")
        else:
            click.echo("    ⚠ No edges resolved — check that nodes/files exist in the index")
        click.echo()

    db.close()


@main.command()
@click.argument("prompt")
@click.option("--budget", type=int, default=16000, help="Token budget")
@click.option("--hints", multiple=True, help="Change hints (natural language)")
@click.option("--change-type", type=str, default=None, help="Explicit change type")
@click.option("--seeds", multiple=True, help="Explicit seed node IDs")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def query(prompt, budget, hints, change_type, seeds, as_json):
    """Get task-adaptive context for a prompt."""
    root = Path.cwd().resolve()
    db = EngramDB(root)

    if not db.exists:
        click.echo("Error: No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)

    # Freshness check — re-index stale files
    extensions = get_all_extensions()
    files = scan_project(root, extensions)
    current_hashes = {}
    for rel_path in files:
        abs_path = root / rel_path
        current_hashes[str(rel_path)] = hash_file(abs_path)

    changed, new, deleted = store.get_stale_files(current_hashes)
    if changed or new or deleted:
        stale_count = len(changed) + len(new) + len(deleted)
        click.echo(f"Refreshing {stale_count} stale file(s)...", err=True)
        build_index(root, db, force=False)

    # v1: Wire up memory search
    from engram.memory.search import MemorySearch
    mem_search = MemorySearch(store)

    assembler = ContextAssembler(store, project_root=root, memory_search=mem_search)
    package = assembler.assemble(
        prompt=prompt,
        max_tokens=budget,
        change_hints=list(hints) if hints else None,
        change_type=change_type,
        seeds=list(seeds) if seeds else None,
    )

    if as_json:
        import json
        click.echo(json.dumps({
            "task": package.task,
            "change_types": sorted(package.change_types),
            "seeds": package.seeds,
            "total_tokens": package.total_tokens,
            "budget": package.budget,
            "stats": package.stats,
            "nodes": [
                {
                    "id": cn.node.id,
                    "detail_level": cn.detail_level,
                    "priority": cn.priority,
                    "depth": cn.depth,
                    "tokens": cn.token_estimate,
                    "content": cn.content,
                }
                for cn in package.nodes
            ],
        }, indent=2))
    else:
        click.echo(package.serialize())

    db.close()


@main.command()
def status():
    """Show project index status."""
    root = Path.cwd().resolve()
    db = EngramDB(root)

    if not db.exists:
        click.echo("No index found. Run `engram build` first.")
        return

    store = GraphStore(db)
    stats = store.get_stats()

    # Check stale files
    extensions = get_all_extensions()
    files = scan_project(root, extensions)
    current_hashes = {str(p): hash_file(root / p) for p in files}
    changed, new, deleted = store.get_stale_files(current_hashes)

    click.echo(f"Project: {root.name}")
    click.echo(f"Index:   {stats['file_count']} files, {stats['node_count']} nodes, {stats['edge_count']} edges")
    click.echo(f"         {stats['function_count']} functions, {stats['class_count']} classes")
    click.echo(f"Languages: {', '.join(stats['languages']) if stats['languages'] else 'none'}")

    stale = len(changed) + len(new) + len(deleted)
    if stale:
        click.echo(f"Stale:   {stale} file(s) need re-indexing")
        for f in changed[:5]:
            click.echo(f"  changed: {f}")
        for f in new[:5]:
            click.echo(f"  new: {f}")
        for f in deleted[:5]:
            click.echo(f"  deleted: {f}")
    else:
        click.echo("Status:  up to date")

    db.close()


@main.command("export")
@click.option("--format", "fmt", type=click.Choice(["md", "json"]), default="md")
@click.option("--output", "-o", type=click.Path(), default=None, help="Output file (default: stdout)")
def export_cmd(fmt, output):
    """Export project brain as a static file."""
    root = Path.cwd().resolve()
    db = EngramDB(root)

    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)

    from engram.export import export_brain
    result = export_brain(store, format=fmt)

    if output:
        Path(output).write_text(result)
        click.echo(f"Exported to {output}")
    else:
        click.echo(result)

    db.close()


# --- v3 commands ---

@main.command()
@click.argument("diff_file", required=False, type=click.Path(exists=True))
@click.option("--stdin", "from_stdin", is_flag=True, help="Read diff from stdin")
@click.option("--change-type", multiple=True, help="Explicit change types")
@click.option("--seeds", multiple=True, help="Explicit seed node IDs")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--fail-on-incomplete", is_flag=True, help="Exit code 1 if INCOMPLETE")
def verify(diff_file, from_stdin, change_type, seeds, as_json, fail_on_incomplete):
    """Verify structural completeness of a code patch."""
    import subprocess
    from engram.verification.verifier import Verifier, Verdict
    from engram.verification.followup import FollowUpGenerator
    from engram.verification.feedback import RetrievalFeedback

    if from_stdin:
        diff_text = sys.stdin.read()
    elif diff_file:
        diff_text = Path(diff_file).read_text()
    else:
        # Default: uncommitted changes
        try:
            diff_text = subprocess.check_output(["git", "diff"], text=True)
            if not diff_text:
                diff_text = subprocess.check_output(["git", "diff", "--cached"], text=True)
        except Exception:
            click.echo("No diff provided and git not available. Pass a diff file or use --stdin.", err=True)
            sys.exit(1)

    if not diff_text.strip():
        click.echo("Empty diff — nothing to verify.")
        return

    root = Path.cwd().resolve()
    db = EngramDB(root)
    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)
    verifier = Verifier(store)

    result = verifier.verify(
        diff_text=diff_text,
        seeds=list(seeds) if seeds else None,
        change_types=set(change_type) if change_type else None,
    )

    # Record feedback
    try:
        feedback = RetrievalFeedback(store)
        feedback.record(result)
    except Exception:
        pass

    if as_json:
        import json as json_mod
        click.echo(json_mod.dumps(result.to_dict(), indent=2))
    else:
        if result.verdict == Verdict.STRUCTURALLY_COMPLETE:
            click.echo(f"✓ Patch is STRUCTURALLY COMPLETE ({result.stats['touched_count']} nodes verified)")
        else:
            gen = FollowUpGenerator()
            click.echo(gen.generate(result))

    if fail_on_incomplete and result.verdict == Verdict.INCOMPLETE:
        sys.exit(1)

    db.close()


# --- v1 commands ---

@main.command()
@click.option("--refresh", is_flag=True, help="Force regenerate snapshot")
def snapshot(refresh):
    """View or regenerate the project architectural snapshot."""
    root = Path.cwd().resolve()
    db = EngramDB(root)

    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)
    from engram.memory.sessions import SessionManager
    from engram.memory.observations import ObservationManager
    from engram.snapshot import SnapshotGenerator

    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    project = root.name

    gen = SnapshotGenerator(store, obs_mgr, project)
    content = gen.get_or_generate(force_refresh=refresh)
    click.echo(content)


@main.command()
def mcp():
    """Start the MCP server (stdio transport)."""
    from engram.mcp import main as mcp_main
    mcp_main()


@main.command("search")
@click.argument("query")
@click.option("--type", "obs_type", type=click.Choice(["decision", "bugfix", "architecture", "discovery", "preference", "issue"]))
@click.option("--limit", type=int, default=10)
@click.option("--full", is_flag=True, help="Show full content instead of snippets")
def search_cmd(query, obs_type, limit, full):
    """Search project memories."""
    root = Path.cwd().resolve()
    db = EngramDB(root)

    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)
    from engram.memory.search import MemorySearch
    mem_search = MemorySearch(store)

    if full:
        results = mem_search.search(query, type=obs_type, limit=limit)
        for i, r in enumerate(results, 1):
            click.echo(f"{i}. [{r.get('type', '')}] {r.get('title', '')}")
            click.echo(f"   {r.get('content', '')}")
            click.echo()
    else:
        results = mem_search.search_progressive(query, type=obs_type, limit=limit)
        for i, r in enumerate(results, 1):
            click.echo(f'{i}. [{r.get("type", "")}] {r.get("title", "")}')
            click.echo(f'   "{r.get("snippet", "")}" ({r.get("linked_nodes", 0)} linked nodes)')
            click.echo()

    if not results:
        click.echo("No matching observations found.")

    db.close()


@main.command("save")
@click.argument("title")
@click.argument("content")
@click.option("--type", "obs_type", required=True,
              type=click.Choice(["decision", "bugfix", "architecture", "discovery", "preference", "issue"]))
@click.option("--topic-key", type=str, default=None)
@click.option("--node-ids", multiple=True)
def save_cmd(title, content, obs_type, topic_key, node_ids):
    """Save a project observation."""
    root = Path.cwd().resolve()
    db = EngramDB(root)
    store = GraphStore(db)

    from engram.memory.sessions import SessionManager
    from engram.memory.observations import ObservationManager

    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)

    obs_id = obs_mgr.save(
        title=title,
        content=content,
        type=obs_type,
        project=root.name,
        topic_key=topic_key,
        node_ids=list(node_ids) if node_ids else None,
    )

    obs = obs_mgr.get(obs_id)
    node_count = len(obs.get("linked_nodes", [])) if obs else 0
    click.echo(f'Saved observation #{obs_id}: "{title}" ({node_count} linked nodes)')

    db.close()


# --- v2 commands ---

@main.command()
@click.option("--root", type=click.Path(exists=True), default=".", help="Project root")
def watch(root):
    """Watch for file changes and auto-rebuild index."""
    from engram.watch.watcher import EngramWatcher
    root_path = Path(root).resolve()
    db = EngramDB(root_path)

    if not db.exists:
        click.echo("No index found. Running initial build...")
        build_index(root_path, db, force=True)

    watcher = EngramWatcher(root_path, db)
    watcher.start()


@main.command()
@click.argument("export_file", type=click.Path(exists=True))
@click.option("--project", type=str, default=None, help="Project name (auto-detect if omitted)")
def journal(export_file, project):
    """Extract observations from a conversation export file."""
    from engram.journal.parser import ExportParser
    from engram.journal.extractor import JournalExtractor
    from engram.memory.sessions import SessionManager
    from engram.memory.observations import ObservationManager

    root = Path.cwd().resolve()
    db = EngramDB(root)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)

    parser = ExportParser()
    conversations = parser.parse(Path(export_file))
    extractor = JournalExtractor(obs_mgr)
    project_name = project or root.name
    obs_ids = extractor.extract_and_save(conversations, project_name)
    click.echo(f"Extracted {len(obs_ids)} observations from {len(conversations)} conversations")

    db.close()


@main.group()
def sync():
    """Push/pull memories for team sharing via git."""


@sync.command()
def push():
    """Export memories to .engram/sync/ for git commit."""
    from engram.sync.exporter import MemoryExporter

    root = Path.cwd().resolve()
    db = EngramDB(root)
    store = GraphStore(db)
    project = root.name
    exporter = MemoryExporter(store, project)
    sync_dir = root / ".engram" / "sync"
    exporter.export_to_jsonl(sync_dir)
    click.echo(f"Exported to {sync_dir}/ — commit and push to share")
    db.close()


@sync.command()
def pull():
    """Import memories from .engram/sync/ (after git pull)."""
    from engram.sync.importer import MemoryImporter
    from engram.memory.sessions import SessionManager
    from engram.memory.observations import ObservationManager

    root = Path.cwd().resolve()
    db = EngramDB(root)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    importer = MemoryImporter(store, obs_mgr)
    sync_dir = root / ".engram" / "sync"
    if not sync_dir.exists():
        click.echo("No sync data found. Run `engram sync push` first or `git pull`.")
        return
    result = importer.import_from_jsonl(sync_dir)
    click.echo(f"Imported: {result['imported']}, Updated: {result['updated']}, Skipped: {result['skipped']}")
    db.close()


# --- v4 commands ---

@main.command()
@click.argument("task_dir", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), default="benchmarks/results")
def bench(task_dir, output):
    """Run benchmark suite against task files."""
    from engram.bench import load_task, BenchmarkRunner, BenchmarkEvaluator, BenchmarkReporter
    from engram.memory.search import MemorySearch

    root = Path.cwd().resolve()
    db = EngramDB(root)
    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)
    mem_search = MemorySearch(store)
    assembler = ContextAssembler(store, project_root=root, memory_search=mem_search)
    runner = BenchmarkRunner(store, assembler)
    evaluator = BenchmarkEvaluator()
    reporter = BenchmarkReporter()

    task_path = Path(task_dir)
    task_files = sorted(task_path.glob("*.yaml")) + sorted(task_path.glob("*.yml"))
    if not task_files:
        click.echo(f"No task files found in {task_dir}")
        return

    tasks = []
    all_evals = []
    for tf in task_files:
        task = load_task(tf)
        tasks.append(task)
        results = runner.run_task(task)
        evals = evaluator.evaluate(results, task)
        all_evals.append(evals)
        click.echo(f"  {task.get('id', tf.stem)}: {len(results)} methods benchmarked")

    report = reporter.generate_report(all_evals, tasks, Path(output))
    click.echo(f"\nReport generated at {output}/report.md")
    db.close()


@main.command()
@click.option("--provider", type=click.Choice(["anthropic", "openai"]), default="anthropic")
@click.option("--force", is_flag=True, help="Re-summarize nodes with existing LLM summaries")
def summarize(provider, force):
    """Generate LLM-enhanced summaries for code nodes."""
    from engram.summarizer import LLMSummarizer, BatchSummarizer

    root = Path.cwd().resolve()
    db = EngramDB(root)
    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)
    summarizer = LLMSummarizer(provider=provider)
    batch = BatchSummarizer(store, summarizer)
    result = batch.summarize_project(force=force)

    click.echo(f"Total: {result.total}, Summarized: {result.summarized}, Skipped: {result.skipped}")
    click.echo(f"Estimated cost: ${result.cost_estimate}")
    db.close()


@main.command()
@click.option("--port", type=int, default=8080, help="Port for the UI server")
def ui(port):
    """Launch the graph browser web UI."""
    from engram.ui import run_ui

    root = Path.cwd().resolve()
    db = EngramDB(root)
    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    run_ui(root, port)


@main.group()
def patterns():
    """Manage cross-project structural patterns."""


@patterns.command("list")
def patterns_list():
    """List detected patterns in the current project."""
    from engram.patterns import PatternDetector

    root = Path.cwd().resolve()
    db = EngramDB(root)
    if not db.exists:
        click.echo("No index found. Run `engram build` first.", err=True)
        sys.exit(1)

    store = GraphStore(db)
    detector = PatternDetector(store)
    framework = detector.detect_framework()
    click.echo(f"Framework: {framework or 'none detected'}")

    matched = detector.detect_patterns()
    if matched:
        for p in matched:
            click.echo(f"  [{p.confidence:.0%}] {p.name}: {p.description}")
    else:
        click.echo("  No patterns matched.")
    db.close()


@patterns.command("export")
@click.argument("output_file", type=click.Path())
def patterns_export(output_file):
    """Export patterns to a portable JSON file."""
    from engram.patterns import PatternCatalog

    root = Path.cwd().resolve()
    db = EngramDB(root)
    store = GraphStore(db)
    catalog = PatternCatalog(store)
    catalog.export_patterns(Path(output_file))
    click.echo(f"Exported to {output_file}")
    db.close()


@patterns.command("import")
@click.argument("input_file", type=click.Path(exists=True))
def patterns_import(input_file):
    """Import patterns from a JSON file."""
    from engram.patterns import PatternCatalog

    root = Path.cwd().resolve()
    db = EngramDB(root)
    store = GraphStore(db)
    catalog = PatternCatalog(store)
    count = catalog.import_patterns(Path(input_file))
    click.echo(f"Imported {count} new patterns.")
    db.close()


if __name__ == "__main__":
    main()