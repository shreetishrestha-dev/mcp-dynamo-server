"""PartiQL statement and batch tests.

Note: moto's PartiQL parser doesn't substitute positional ``?`` parameters in
``INSERT`` / ``DELETE`` statements (it works for ``SELECT``). Real DynamoDB
supports it. The tests below mix parameterized SELECTs with literal-value
INSERTs/DELETEs to exercise our code under the mock's limitations.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_partiql_select_with_parameters(call):
    await call("put_item", table_name="Users", item={"id": "u_1", "name": "Ada"})
    await call("put_item", table_name="Users", item={"id": "u_2", "name": "Grace"})
    result = await call(
        "execute_partiql_statement",
        statement="SELECT * FROM Users WHERE id = ?",
        parameters=["u_1"],
    )
    assert result["count"] == 1
    assert result["items"][0]["name"] == "Ada"


async def test_partiql_insert_literal(call):
    """INSERT with literal values (mock limitation: ? not bound in INSERT)."""
    result = await call(
        "execute_partiql_statement",
        statement="INSERT INTO Users VALUE {'id': 'u_99', 'name': 'Inserted'}",
    )
    assert result is not None
    got = await call("get_item", table_name="Users", key={"id": "u_99"})
    assert got["item"]["name"] == "Inserted"


async def test_partiql_select_no_match(call):
    result = await call(
        "execute_partiql_statement",
        statement="SELECT * FROM Users WHERE id = ?",
        parameters=["missing"],
    )
    assert result["count"] == 0
    assert result["items"] == []


async def test_partiql_batch_inserts_literal(call):
    """Batch of literal INSERTs (mock limitation: ? not bound in INSERT)."""
    result = await call(
        "execute_partiql_batch",
        statements=[
            {"Statement": "INSERT INTO Users VALUE {'id': 'u_10', 'name': 'A'}"},
            {"Statement": "INSERT INTO Users VALUE {'id': 'u_11', 'name': 'B'}"},
        ],
    )
    assert result["ok"] is True
    a = await call("get_item", table_name="Users", key={"id": "u_10"})
    b = await call("get_item", table_name="Users", key={"id": "u_11"})
    assert a["item"]["name"] == "A"
    assert b["item"]["name"] == "B"
