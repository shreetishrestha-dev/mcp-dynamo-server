"""Tests for discovery tools: list_tables, describe_table, infer_schema.

Covers:
- infer_schema basic inference
- infer_schema on empty table (0 items)
- infer_schema access control denial
- infer_schema GSI hints
- infer_schema sample_values for low-cardinality string attributes
"""

from __future__ import annotations

import pytest
import boto3

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def orders_table_with_gsi(ddb):
    """Create an Orders table with a GSI on 'status'."""
    ddb.create_table(
        TableName="Orders",
        KeySchema=[
            {"AttributeName": "order_id", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "order_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "status-index",
                "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    waiter = ddb.get_waiter("table_exists")
    waiter.wait(TableName="Orders")
    return "Orders"


@pytest.fixture
def simple_table(ddb):
    """Create a simple Users table with no GSIs."""
    ddb.create_table(
        TableName="Users",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = ddb.get_waiter("table_exists")
    waiter.wait(TableName="Users")
    return "Users"


@pytest.fixture
def server_with_tables(simple_table, orders_table_with_gsi):
    from mcp_dynamo.server import build_server
    return build_server()


@pytest.fixture
def call_discovery(server_with_tables):
    from tests.conftest import _call

    async def _bound(name: str, **kwargs):
        return await _call(server_with_tables, name, **kwargs)

    return _bound


# ---------------------------------------------------------------------------
# infer_schema basic inference
# ---------------------------------------------------------------------------


async def test_infer_schema_basic_with_items(ddb, call_discovery, simple_table):
    """infer_schema returns correct structure for a table with items."""
    # Seed some items via low-level DynamoDB
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    table = resource.Table("Users")
    for i in range(3):
        table.put_item(Item={"id": f"u_{i}", "name": f"User {i}", "age": i + 20})

    result = await call_discovery("infer_schema", table_name="Users")

    assert result["table"] == "Users"
    assert result["sampled_items"] == 3
    assert "key_schema" in result
    assert "pk" in result["key_schema"]
    assert "id" in result["key_schema"]["pk"]

    assert "attributes" in result
    attrs = result["attributes"]
    assert "id" in attrs
    assert attrs["id"]["type"] == "S"
    assert attrs["id"]["present_in"] == "100%"
    assert "name" in attrs
    assert "age" in attrs

    assert isinstance(result["gsi_hints"], list)
    assert result["rendered"] is None  # default format="json"


async def test_infer_schema_empty_table(ddb, call_discovery, simple_table):
    """infer_schema on a table with no items returns sampled_items=0 and empty attributes."""
    result = await call_discovery("infer_schema", table_name="Users")
    assert result["sampled_items"] == 0
    assert result["attributes"] == {}
    assert isinstance(result["gsi_hints"], list)


async def test_infer_schema_gsi_hints(ddb, call_discovery, orders_table_with_gsi):
    """infer_schema includes GSI hints for tables with global secondary indexes."""
    result = await call_discovery("infer_schema", table_name="Orders")

    assert len(result["gsi_hints"]) >= 1
    gsi_hint = result["gsi_hints"][0]
    assert "status-index" in gsi_hint
    assert "status" in gsi_hint


async def test_infer_schema_key_schema_shape(ddb, call_discovery, orders_table_with_gsi):
    """infer_schema key_schema includes pk and sk with type annotations."""
    result = await call_discovery("infer_schema", table_name="Orders")
    assert "pk" in result["key_schema"]
    assert "sk" in result["key_schema"]
    assert "order_id" in result["key_schema"]["pk"]
    assert "S" in result["key_schema"]["pk"]
    assert "created_at" in result["key_schema"]["sk"]
    assert "N" in result["key_schema"]["sk"]


async def test_infer_schema_sample_values_low_cardinality(ddb, call_discovery, simple_table):
    """String attributes with <= 10 distinct values include sample_values."""
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    table = resource.Table("Users")
    statuses = ["ACTIVE", "INACTIVE", "PENDING"]
    for i in range(9):
        table.put_item(Item={
            "id": f"u_{i}",
            "status": statuses[i % len(statuses)],
        })

    result = await call_discovery("infer_schema", table_name="Users")
    attrs = result["attributes"]
    assert "status" in attrs
    assert "sample_values" in attrs["status"]
    assert set(attrs["status"]["sample_values"]) == set(statuses)


async def test_infer_schema_sample_values_high_cardinality(ddb, call_discovery, simple_table):
    """String attributes with > 10 distinct values do NOT include sample_values."""
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    table = resource.Table("Users")
    # 11 distinct email values
    for i in range(11):
        table.put_item(Item={"id": f"u_{i}", "email": f"user{i}@example.com"})

    result = await call_discovery("infer_schema", table_name="Users")
    attrs = result["attributes"]
    assert "email" in attrs
    assert "sample_values" not in attrs["email"]


async def test_infer_schema_table_format(ddb, call_discovery, simple_table):
    """With format='table', the result includes a non-None rendered field."""
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    table = resource.Table("Users")
    table.put_item(Item={"id": "u_1", "name": "Ada"})

    result = await call_discovery("infer_schema", table_name="Users", format="table")
    assert result["rendered"] is not None
    assert isinstance(result["rendered"], str)
    assert len(result["rendered"]) > 0


# ---------------------------------------------------------------------------
# infer_schema access control denial
# ---------------------------------------------------------------------------


async def test_infer_schema_access_control_denial(monkeypatch, ddb, call_discovery, simple_table):
    """infer_schema respects the access control list."""
    monkeypatch.setenv("DYNAMODB_ALLOWED_TABLES", "Orders")  # Users excluded
    from mcp_dynamo import client as client_module
    client_module.reset_clients()

    result = await call_discovery("infer_schema", table_name="Users")
    assert result["error"] == "TableAccessDenied"
    assert result["table"] == "Users"

    client_module.reset_clients()


# ---------------------------------------------------------------------------
# infer_schema error handling
# ---------------------------------------------------------------------------


async def test_infer_schema_nonexistent_table_returns_error(aws, server_with_tables, call_discovery):
    """infer_schema on a missing table returns a ResourceNotFoundException error."""
    result = await call_discovery("infer_schema", table_name="NoSuchTable")
    assert result.get("error") == "ResourceNotFoundException"
