"""Engram MCP server entry point. Run with: python -m engram.mcp"""

import asyncio
from engram.mcp.server import EngramMCPServer


def main():
    server = EngramMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
