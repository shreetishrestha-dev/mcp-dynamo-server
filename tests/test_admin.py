"""Table lifecycle tests: list, describe, create, update, delete."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_list_tables_includes_seed(call):
    result = await call("list_tables")
    assert "Users" in result["tables"]
    assert "Orders" in result["tables"]
    assert result["count"] == 2


async def test_list_tables_prefix_filter(call):
    result = await call("list_tables", prefix="Use")
    assert result["tables"] == ["Users"]


async def test_describe_table(call):
    result = await call("describe_table", table_name="Orders")
    schema = result["KeySchema"]
    names = {k["AttributeName"]: k["KeyType"] for k in schema}
    assert names == {"user_id": "HASH", "order_id": "RANGE"}


async def test_create_table_lifecycle(call):
    create = await call(
        "create_table",
        table_name="Tasks",
        key_schema=[{"AttributeName": "id", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "id", "AttributeType": "S"}],
    )
    assert create["ok"] is True
    listed = await call("list_tables")
    assert "Tasks" in listed["tables"]

    delete = await call("delete_table", table_name="Tasks", confirm=True)
    assert delete["ok"] is True
    listed2 = await call("list_tables")
    assert "Tasks" not in listed2["tables"]


async def test_dynamo_whoami(call):
    result = await call("dynamo_whoami")
    assert result["region"] == "us-east-1"
    assert result["is_local"] is False
