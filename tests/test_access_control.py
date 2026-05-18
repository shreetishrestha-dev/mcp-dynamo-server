"""Tests for the table allow/block list access control.

Covers: allowed list (permit + deny), blocked list (permit + deny),
both-set (allowed list wins), list_tables filtering.
"""

from __future__ import annotations

import pytest

from mcp_dynamo.access_control import check_table_access
from mcp_dynamo.config import Config


# ---------------------------------------------------------------------------
# Unit tests for check_table_access
# ---------------------------------------------------------------------------


def _cfg(
    *,
    allowed_tables: frozenset[str] | None = None,
    blocked_tables: frozenset[str] | None = None,
) -> Config:
    """Build a minimal Config for access-control tests."""
    return Config(
        region="us-east-1",
        allowed_tables=allowed_tables,
        blocked_tables=blocked_tables,
    )


def test_unrestricted_allows_any_table():
    cfg = _cfg()
    assert check_table_access("Users", cfg) is None
    assert check_table_access("Orders", cfg) is None
    assert check_table_access("SomeRandom", cfg) is None


def test_allowed_list_permits_listed_table():
    cfg = _cfg(allowed_tables=frozenset({"Users", "Orders"}))
    assert check_table_access("Users", cfg) is None
    assert check_table_access("Orders", cfg) is None


def test_allowed_list_denies_unlisted_table():
    cfg = _cfg(allowed_tables=frozenset({"Users", "Orders"}))
    result = check_table_access("BillingRecords", cfg)
    assert result is not None
    assert result["error"] == "TableAccessDenied"
    assert result["table"] == "BillingRecords"
    assert "BillingRecords" in result["message"]


def test_blocked_list_permits_unlisted_table():
    cfg = _cfg(blocked_tables=frozenset({"BillingRecords", "AuditLog"}))
    assert check_table_access("Users", cfg) is None
    assert check_table_access("Orders", cfg) is None


def test_blocked_list_denies_listed_table():
    cfg = _cfg(blocked_tables=frozenset({"BillingRecords", "AuditLog"}))
    result = check_table_access("BillingRecords", cfg)
    assert result is not None
    assert result["error"] == "TableAccessDenied"
    assert result["table"] == "BillingRecords"


def test_both_set_allowed_takes_precedence():
    """When both allowed_tables and blocked_tables are set, allowed_tables governs."""
    # "Users" is in allowed; also in blocked — allowed wins, so access is granted.
    cfg = _cfg(
        allowed_tables=frozenset({"Users"}),
        blocked_tables=frozenset({"Users", "Orders"}),
    )
    # Users is in allowed_tables → permitted
    assert check_table_access("Users", cfg) is None
    # Orders is NOT in allowed_tables → denied (even though it's in blocked too)
    result = check_table_access("Orders", cfg)
    assert result is not None
    assert result["error"] == "TableAccessDenied"


def test_error_response_shape():
    """The error dict must include error, table, and message keys."""
    cfg = _cfg(allowed_tables=frozenset({"Users"}))
    result = check_table_access("Nope", cfg)
    assert isinstance(result, dict)
    assert set(result.keys()) >= {"error", "table", "message"}
    assert result["error"] == "TableAccessDenied"
    assert result["table"] == "Nope"
    assert isinstance(result["message"], str)


# ---------------------------------------------------------------------------
# Integration tests: access control wired into tool handlers
# ---------------------------------------------------------------------------


async def test_get_item_denied_by_allowed_list(monkeypatch, call, seed_tables):
    """get_item on a table not in allowed_tables returns TableAccessDenied."""
    monkeypatch.setenv("DYNAMODB_ALLOWED_TABLES", "Orders")  # Users is excluded
    from mcp_dynamo import client as client_module
    client_module.reset_clients()

    result = await call("get_item", table_name="Users", key={"id": "u_1"})
    assert result["error"] == "TableAccessDenied"
    assert result["table"] == "Users"

    client_module.reset_clients()


async def test_get_item_permitted_by_allowed_list(monkeypatch, call, seed_tables):
    """get_item on an allowed table works normally."""
    monkeypatch.setenv("DYNAMODB_ALLOWED_TABLES", "Users,Orders")
    from mcp_dynamo import client as client_module
    client_module.reset_clients()

    # Put an item first
    await call("put_item", table_name="Users", item={"id": "u_1", "name": "Ada"})
    result = await call("get_item", table_name="Users", key={"id": "u_1"})
    # Should succeed — no error key
    assert "error" not in result or result.get("error") is None
    assert result.get("item") is not None

    client_module.reset_clients()


async def test_scan_denied_by_blocked_list(monkeypatch, call, seed_tables):
    """scan on a blocked table returns TableAccessDenied."""
    monkeypatch.setenv("DYNAMODB_BLOCKED_TABLES", "Users")
    from mcp_dynamo import client as client_module
    client_module.reset_clients()

    result = await call("scan", table_name="Users")
    assert result["error"] == "TableAccessDenied"

    client_module.reset_clients()


async def test_list_tables_filters_denied_tables(monkeypatch, call, seed_tables):
    """list_tables omits tables that would be denied by access control."""
    monkeypatch.setenv("DYNAMODB_ALLOWED_TABLES", "Orders")
    from mcp_dynamo import client as client_module
    client_module.reset_clients()

    result = await call("list_tables")
    # Users should not appear because it's not in the allowed list
    assert "Users" not in result["tables"]
    # Orders should appear
    assert "Orders" in result["tables"]

    client_module.reset_clients()


async def test_list_tables_no_restriction_shows_all(call, seed_tables):
    """Without access control, list_tables returns all tables."""
    result = await call("list_tables")
    assert "Users" in result["tables"]
    assert "Orders" in result["tables"]


async def test_describe_table_denied(monkeypatch, call, seed_tables):
    monkeypatch.setenv("DYNAMODB_BLOCKED_TABLES", "Users")
    from mcp_dynamo import client as client_module
    client_module.reset_clients()

    result = await call("describe_table", table_name="Users")
    assert result["error"] == "TableAccessDenied"

    client_module.reset_clients()
