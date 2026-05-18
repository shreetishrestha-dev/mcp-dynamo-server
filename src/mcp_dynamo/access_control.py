"""Table-level access control for the DynamoDB MCP server.

Enforces the allow/block list configured via ``DYNAMODB_ALLOWED_TABLES`` and
``DYNAMODB_BLOCKED_TABLES`` environment variables. Called at the tool-handler
boundary, before any AWS call is made.
"""

from __future__ import annotations

from mcp_dynamo.config import Config


def check_table_access(table_name: str, cfg: Config) -> dict | None:
    """Return ``None`` if access is allowed, or an error dict if denied.

    Precedence rules (mirrors ``config.py``):
    1. If ``allowed_tables`` is set — only listed tables are accessible.
    2. Else if ``blocked_tables`` is set — listed tables are refused.
    3. If neither is set — unrestricted.

    The error dict shape is::

        {
            "error": "TableAccessDenied",
            "table": "<table_name>",
            "message": "Table '<table_name>' is not accessible via this server instance."
        }
    """
    if cfg.allowed_tables is not None:
        if table_name not in cfg.allowed_tables:
            return {
                "error": "TableAccessDenied",
                "table": table_name,
                "message": f"Table '{table_name}' is not accessible via this server instance.",
            }
    elif cfg.blocked_tables is not None:
        if table_name in cfg.blocked_tables:
            return {
                "error": "TableAccessDenied",
                "table": table_name,
                "message": f"Table '{table_name}' is not accessible via this server instance.",
            }
    return None


__all__ = ["check_table_access"]
