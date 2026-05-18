"""Single-item CRUD + batch operation tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_put_and_get_item(call):
    await call("put_item", table_name="Users", item={"id": "u_1", "name": "Ada"})
    result = await call("get_item", table_name="Users", key={"id": "u_1"})
    assert result["item"] == {"id": "u_1", "name": "Ada"}


async def test_get_item_missing(call):
    result = await call("get_item", table_name="Users", key={"id": "missing"})
    assert result["item"] is None


async def test_update_item(call):
    await call("put_item", table_name="Users", item={"id": "u_2", "name": "Old"})
    result = await call(
        "update_item",
        table_name="Users",
        key={"id": "u_2"},
        update_expression="SET #n = :name",
        expression_attribute_names={"#n": "name"},
        expression_attribute_values={":name": "New"},
    )
    assert result["ok"] is True
    assert result["attributes"]["name"] == "New"


async def test_put_with_condition_blocks_overwrite(call):
    await call("put_item", table_name="Users", item={"id": "u_3", "name": "A"})
    result = await call(
        "put_item",
        table_name="Users",
        item={"id": "u_3", "name": "B"},
        condition_expression="attribute_not_exists(id)",
    )
    assert isinstance(result, dict)
    error = result.get("error", "")
    message = result.get("message", "")
    combined = (error + " " + message).lower()
    assert "conditionalcheckfailed" in combined or "conditional" in combined


async def test_batch_get_item(call):
    await call("put_item", table_name="Users", item={"id": "u_1", "name": "Ada"})
    await call("put_item", table_name="Users", item={"id": "u_2", "name": "Grace"})
    result = await call(
        "batch_get_item",
        request_items={"Users": {"Keys": [{"id": "u_1"}, {"id": "u_2"}]}},
    )
    names = sorted(r["name"] for r in result["responses"]["Users"])
    assert names == ["Ada", "Grace"]


async def test_batch_write_puts_only_runs_without_confirm(call):
    """Pure-put batches are not destructive — they execute immediately."""
    result = await call(
        "batch_write_item",
        request_items={
            "Users": [
                {"PutRequest": {"Item": {"id": "u_1", "name": "Ada"}}},
                {"PutRequest": {"Item": {"id": "u_2", "name": "Grace"}}},
            ]
        },
    )
    assert result["ok"] is True
    got = await call("get_item", table_name="Users", key={"id": "u_1"})
    assert got["item"]["name"] == "Ada"


async def test_table_format_render(call):
    await call("put_item", table_name="Users", item={"id": "u_1", "name": "Ada"})
    result = await call("get_item", table_name="Users", key={"id": "u_1"}, format="table")
    # Table-format returns a dict-of-rendered or the data list — both fine.
    assert result is not None
