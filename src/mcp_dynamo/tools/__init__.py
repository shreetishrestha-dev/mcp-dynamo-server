"""Tool implementations grouped by domain.

Each module exports a single ``register(mcp)`` function that attaches its tools
to the FastMCP instance. ``server.py`` calls them in order.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_dynamo.tools import admin, discovery, items, partiql, queries


def register_all(mcp: FastMCP) -> None:
    discovery.register(mcp)
    items.register(mcp)
    queries.register(mcp)
    partiql.register(mcp)
    admin.register(mcp)


__all__ = ["register_all"]
