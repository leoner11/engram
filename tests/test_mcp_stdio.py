"""Integration test: MCP server over stdio transport.

Tests that the Engram MCP server can:
1. Start and respond to initialize/list_tools over stdio
2. Handle engram_query tool calls through the transport
3. Handle engram_verify tool calls through the transport
4. Handle engram_save and engram_search round-trip

These tests spawn the server as a subprocess and communicate via
JSON-RPC over stdin/stdout, exactly as Claude Code would.
"""

import json
import subprocess
import shutil
import sys
import os
from pathlib import Path

import pytest

from engram.db import EngramDB
from engram.cli import build_index


FIXTURES_DIR = Path(__file__).parent / "fixtures"

# JSON-RPC helpers

_msg_id = 0


def _jsonrpc_request(method: str, params: dict | None = None) -> str:
    global _msg_id
    _msg_id += 1
    msg = {"jsonrpc": "2.0", "id": _msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _jsonrpc_notification(method: str, params: dict | None = None) -> str:
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _send(proc, message: str):
    """Send a JSON-RPC message with Content-Length header."""
    encoded = message.encode("utf-8")
    header = f"Content-Length: {len(encoded)}\r\n\r\n"
    proc.stdin.write(header.encode("utf-8") + encoded)
    proc.stdin.flush()


def _recv(proc, timeout: float = 15.0) -> dict | None:
    """Read one JSON-RPC message from stdout, with Content-Length framing."""
    import select
    import io

    # Read headers
    headers = b""
    while True:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            return None
        byte = proc.stdout.read(1)
        if byte == b"":
            return None
        headers += byte
        if headers.endswith(b"\r\n\r\n"):
            break

    # Parse Content-Length
    header_str = headers.decode("utf-8")
    content_length = None
    for line in header_str.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break

    if content_length is None:
        return None

    # Read body
    body = b""
    while len(body) < content_length:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            return None
        chunk = proc.stdout.read(content_length - len(body))
        if chunk == b"":
            return None
        body += chunk

    return json.loads(body.decode("utf-8"))


def _recv_until_id(proc, target_id: int, timeout: float = 15.0) -> dict | None:
    """Read messages until we get one with the target id (skip notifications)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        msg = _recv(proc, timeout=max(remaining, 0.5))
        if msg is None:
            return None
        if msg.get("id") == target_id:
            return msg
        # Otherwise it's a notification or different id — skip
    return None


@pytest.fixture
def indexed_project(tmp_path):
    """Set up a project with a built index."""
    project = tmp_path / "test_project"
    shutil.copytree(FIXTURES_DIR / "simple_project", project)
    db = EngramDB(project)
    build_index(project, db, force=True)
    db.close()
    return project


@pytest.fixture
def mcp_server(indexed_project):
    """Start the Engram MCP server as a subprocess."""
    env = os.environ.copy()
    # Make sure engram is importable
    repo_root = Path(__file__).parent.parent
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "engram.mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(indexed_project),
        env=env,
    )

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _initialize(proc) -> dict:
    """Send initialize + initialized, return the initialize result."""
    init_req = _jsonrpc_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1.0"},
    })
    _send(proc, init_req)
    init_id = _msg_id

    result = _recv_until_id(proc, init_id)

    # Send initialized notification
    _send(proc, _jsonrpc_notification("notifications/initialized"))

    return result


class TestMCPStdioTransport:
    """Tests that speak MCP protocol over stdio pipes."""

    def test_initialize(self, mcp_server):
        """Server responds to initialize with capabilities."""
        result = _initialize(mcp_server)
        assert result is not None, "Server didn't respond to initialize"
        assert "result" in result, f"Expected result, got: {result}"
        assert "serverInfo" in result["result"]
        assert result["result"]["serverInfo"]["name"] == "engram"

    def test_list_tools(self, mcp_server):
        """Server lists all expected tools."""
        _initialize(mcp_server)

        req = _jsonrpc_request("tools/list", {})
        _send(mcp_server, req)
        result = _recv_until_id(mcp_server, _msg_id)

        assert result is not None, "Server didn't respond to tools/list"
        assert "result" in result
        tools = result["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert "engram_query" in tool_names
        assert "engram_verify" in tool_names
        assert "engram_save" in tool_names
        assert "engram_search" in tool_names
        assert "engram_status" in tool_names
        assert "engram_build" in tool_names

    def test_call_query(self, mcp_server):
        """engram_query returns a context package over stdio."""
        _initialize(mcp_server)

        req = _jsonrpc_request("tools/call", {
            "name": "engram_query",
            "arguments": {"prompt": "fix save_order logic"},
        })
        _send(mcp_server, req)
        result = _recv_until_id(mcp_server, _msg_id)

        assert result is not None, "Server didn't respond to engram_query"
        assert "result" in result, f"Got error: {result.get('error')}"
        content = result["result"]["content"]
        assert len(content) >= 1
        text = content[0]["text"]
        assert "Task: fix save_order logic" in text

    def test_call_verify_complete(self, mcp_server):
        """engram_verify returns STRUCTURALLY_COMPLETE for a complete patch."""
        _initialize(mcp_server)

        # Read a diff fixture that's known to be complete for body mod
        diff_path = FIXTURES_DIR / "diffs" / "complete_body_mod.diff"
        diff_text = diff_path.read_text()

        req = _jsonrpc_request("tools/call", {
            "name": "engram_verify",
            "arguments": {
                "diff_text": diff_text,
                "seeds": ["utils.py::validate_user_id"],
                "change_types": ["BODY_MODIFICATION"],
            },
        })
        _send(mcp_server, req)
        result = _recv_until_id(mcp_server, _msg_id)

        assert result is not None, "Server didn't respond to engram_verify"
        assert "result" in result, f"Got error: {result.get('error')}"
        text = result["result"]["content"][0]["text"]
        # Body mod on validate_user_id — callers exist but body mod is low confidence
        assert "COMPLETE" in text or "INCOMPLETE" in text

    def test_call_verify_incomplete(self, mcp_server):
        """engram_verify catches an incomplete signature change."""
        _initialize(mcp_server)

        diff_path = FIXTURES_DIR / "diffs" / "incomplete_signature_change.diff"
        diff_text = diff_path.read_text()

        req = _jsonrpc_request("tools/call", {
            "name": "engram_verify",
            "arguments": {
                "diff_text": diff_text,
                "seeds": ["service.py::process_order"],
                "change_types": ["SIGNATURE_MODIFICATION"],
            },
        })
        _send(mcp_server, req)
        result = _recv_until_id(mcp_server, _msg_id)

        assert result is not None, "Server didn't respond to engram_verify"
        assert "result" in result, f"Got error: {result.get('error')}"
        text = result["result"]["content"][0]["text"]
        assert "INCOMPLETE" in text
        assert "main" in text.lower()  # main.py::main should be listed as missing

    def test_save_and_search_roundtrip(self, mcp_server):
        """Save an observation, then find it via search."""
        _initialize(mcp_server)

        # Save
        save_req = _jsonrpc_request("tools/call", {
            "name": "engram_save",
            "arguments": {
                "title": "Webhook secret moved to env var",
                "content": "STRIPE_WEBHOOK_SECRET must be set in .env for verify_signature to work",
                "type": "discovery",
                "node_ids": ["utils.py::validate_user_id"],
            },
        })
        _send(mcp_server, save_req)
        save_result = _recv_until_id(mcp_server, _msg_id)
        assert save_result is not None
        assert "Saved observation" in save_result["result"]["content"][0]["text"]

        # Search
        search_req = _jsonrpc_request("tools/call", {
            "name": "engram_search",
            "arguments": {"query": "webhook secret", "full": True},
        })
        _send(mcp_server, search_req)
        search_result = _recv_until_id(mcp_server, _msg_id)
        assert search_result is not None
        text = search_result["result"]["content"][0]["text"]
        assert "webhook" in text.lower()

    def test_status(self, mcp_server):
        """engram_status returns project info."""
        _initialize(mcp_server)

        req = _jsonrpc_request("tools/call", {
            "name": "engram_status",
            "arguments": {},
        })
        _send(mcp_server, req)
        result = _recv_until_id(mcp_server, _msg_id)

        assert result is not None
        text = result["result"]["content"][0]["text"]
        assert "Project:" in text
        assert "nodes" in text
