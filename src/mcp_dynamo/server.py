"""FastMCP server bootstrap.

Builds the FastMCP instance, registers every tool, validates credentials, then
runs over stdio. Any startup error (bad config, unreachable endpoint, rejected
creds) is printed to stderr and exits non-zero — that's what the MCP client
displays to the user.
"""

from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP

from mcp_dynamo import __version__
from mcp_dynamo.client import get_config, verify_credentials
from mcp_dynamo.config import ConfigError
from mcp_dynamo.tools import register_all

INSTRUCTIONS = """\
Operate on Amazon DynamoDB tables through this server.

Conventions:
- All keys/items use Python-native dicts; we serialize to DynamoDB types for you.
- Destructive ops (delete_item, delete_table, batch_write_item with deletes,
  PartiQL DELETE) require `confirm=true`. Without it you get a dry-run preview.
- Read tools accept `format: "json" | "table"` (default json).
- query/scan cap at `max_pages` (default 5) and return `last_evaluated_key` to resume.

Run `dynamo_whoami` first if you're unsure which AWS account or endpoint is active.
"""

# Only stdio is supported in v1. Reading from env keeps us forward-compatible:
# when HTTP/SSE lands, this will accept "http" without code churn at the call
# site — but for now anything other than "stdio" must hard-fail so a misconfig
# doesn't silently start an unsupported transport.
SUPPORTED_TRANSPORTS = frozenset({"stdio"})


def _resolve_transport() -> str:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport not in SUPPORTED_TRANSPORTS:
        raise ValueError(
            f"Unsupported MCP_TRANSPORT={transport!r}. "
            f"Supported: {sorted(SUPPORTED_TRANSPORTS)}."
        )
    return transport


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="mcp-dynamo",
        instructions=INSTRUCTIONS,
    )
    register_all(mcp)
    return mcp


def run() -> None:
    # NOTE: stderr is captured and forwarded by MCP hosts (Claude Desktop,
    # Claude Code, Cursor). Never log secrets, credentials, request bodies,
    # or AWS account IDs to stderr — only operational status.
    try:
        transport = _resolve_transport()
    except ValueError as exc:
        print(f"[mcp-dynamo] startup error: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        verify_credentials()
    except ConfigError as exc:
        print(f"[mcp-dynamo] startup error: {exc}", file=sys.stderr)
        sys.exit(2)

    # Log the active access control mode (never log the actual table names at
    # INFO level to avoid leaking configuration details into the MCP transcript).
    cfg = get_config()
    if cfg.allowed_tables is not None:
        print(
            f"[mcp-dynamo] Table access: allowed list ({len(cfg.allowed_tables)} tables)",
            file=sys.stderr,
        )
    elif cfg.blocked_tables is not None:
        print(
            f"[mcp-dynamo] Table access: blocked list ({len(cfg.blocked_tables)} tables)",
            file=sys.stderr,
        )
    else:
        print("[mcp-dynamo] Table access: unrestricted", file=sys.stderr)

    mcp = build_server()
    # NOTE: same stderr guidance as above — version + transport only.
    print(f"[mcp-dynamo {__version__}] starting on {transport}", file=sys.stderr)
    mcp.run(transport)


__all__ = ["build_server", "run"]
