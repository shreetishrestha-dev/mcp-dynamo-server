"""Confirm-flag enforcement.

Every destructive tool must:
  1. Return a dry-run preview (``dry_run: true``, no mutation) when called
     without ``confirm=true``.
  2. Actually execute when called with ``confirm=true``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_delete_item_dry_run_then_execute(call):
    await call("put_item", table_name="Users", item={"id": "u_1", "name": "Ada"})

    preview = await call("delete_item", table_name="Users", key={"id": "u_1"})
    assert preview["dry_run"] is True
    assert preview["action"] == "delete_item"
    assert preview["target"] == {"table_name": "Users", "key": {"id": "u_1"}}

    # Item still exists after dry-run.
    still_there = await call("get_item", table_name="Users", key={"id": "u_1"})
    assert still_there["item"] is not None

    confirmed = await call("delete_item", table_name="Users", key={"id": "u_1"}, confirm=True)
    assert confirmed["ok"] is True

    gone = await call("get_item", table_name="Users", key={"id": "u_1"})
    assert gone["item"] is None


async def test_delete_table_dry_run_then_execute(call):
    preview = await call("delete_table", table_name="Users")
    assert preview["dry_run"] is True
    assert preview["action"] == "delete_table"
    assert preview["target"] == {"table_name": "Users"}

    listed = await call("list_tables")
    assert "Users" in listed["tables"]

    confirmed = await call("delete_table", table_name="Users", confirm=True)
    assert confirmed["ok"] is True

    listed2 = await call("list_tables")
    assert "Users" not in listed2["tables"]


async def test_batch_write_with_delete_requires_confirm(call):
    await call("put_item", table_name="Users", item={"id": "u_x", "name": "X"})

    request_items = {"Users": [{"DeleteRequest": {"Key": {"id": "u_x"}}}]}
    preview = await call("batch_write_item", request_items=request_items)
    assert preview["dry_run"] is True
    assert preview["action"] == "batch_write_item"

    # Item still present.
    still_there = await call("get_item", table_name="Users", key={"id": "u_x"})
    assert still_there["item"] is not None

    confirmed = await call("batch_write_item", request_items=request_items, confirm=True)
    assert confirmed["ok"] is True

    gone = await call("get_item", table_name="Users", key={"id": "u_x"})
    assert gone["item"] is None


async def test_batch_write_pure_puts_skip_confirm(call):
    """Pure-put batches do not need confirm — predicate returns False, decorator passes through."""
    result = await call(
        "batch_write_item",
        request_items={"Users": [{"PutRequest": {"Item": {"id": "p_1"}}}]},
    )
    assert result.get("ok") is True
    got = await call("get_item", table_name="Users", key={"id": "p_1"})
    assert got["item"] is not None


async def test_partiql_delete_requires_confirm(call):
    """DELETE statement gates on confirm.

    Uses a literal DELETE because moto's PartiQL parser does not bind
    positional ``?`` parameters in DELETE; real DynamoDB does. The safety
    check (dry-run preview vs. execute) is what we're verifying here.
    """
    await call("put_item", table_name="Users", item={"id": "u_p", "name": "P"})

    preview = await call(
        "execute_partiql_statement",
        statement="DELETE FROM Users WHERE id = 'u_p'",
    )
    assert preview["dry_run"] is True
    assert preview["action"] == "execute_partiql_statement"

    still_there = await call("get_item", table_name="Users", key={"id": "u_p"})
    assert still_there["item"] is not None

    confirmed = await call(
        "execute_partiql_statement",
        statement="DELETE FROM Users WHERE id = 'u_p'",
        confirm=True,
    )
    # The DELETE returns whatever response shape DynamoDB gives; the key check
    # is that the row is actually gone after confirm.
    assert confirmed is not None
    gone = await call("get_item", table_name="Users", key={"id": "u_p"})
    assert gone["item"] is None


async def test_partiql_select_not_destructive(call):
    """SELECT should run on the first call without confirm."""
    await call("put_item", table_name="Users", item={"id": "u_s", "name": "S"})
    result = await call(
        "execute_partiql_statement",
        statement="SELECT * FROM Users WHERE id = ?",
        parameters=["u_s"],
    )
    assert result.get("count") == 1


async def test_partiql_batch_with_delete_requires_confirm(call):
    await call("put_item", table_name="Users", item={"id": "u_b", "name": "B"})

    statements = [
        {"Statement": "DELETE FROM Users WHERE id = 'u_b'"},
    ]
    preview = await call("execute_partiql_batch", statements=statements)
    assert preview["dry_run"] is True
    assert preview["action"] == "execute_partiql_batch"

    still_there = await call("get_item", table_name="Users", key={"id": "u_b"})
    assert still_there["item"] is not None
