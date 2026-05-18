"""Shared fixtures for moto-backed DynamoDB tests.

Each test runs inside ``moto.mock_aws`` with two seed tables:

- ``Users`` (PK: ``id``)
- ``Orders`` (PK: ``user_id``, SK: ``order_id``)

The fixtures also clear the ``client.py`` lru_caches and set the env vars moto
expects, so each test gets a clean DynamoDB.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from mcp_dynamo import client as client_module


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("DYNAMODB_ENDPOINT_URL", raising=False)
    client_module.reset_clients()
    yield
    client_module.reset_clients()


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def ddb(aws):
    """Low-level DynamoDB client inside the mock."""
    return boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def seed_tables(ddb):
    """Create Users + Orders and return their names."""
    ddb.create_table(
        TableName="Users",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="Orders",
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "order_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "order_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = ddb.get_waiter("table_exists")
    waiter.wait(TableName="Users")
    waiter.wait(TableName="Orders")
    return {"users": "Users", "orders": "Orders"}


@pytest.fixture
def server(seed_tables):
    """A built FastMCP instance with all tools registered against the mock."""
    from mcp_dynamo.server import build_server

    return build_server()


async def _call(server, name: str, **kwargs):
    """Helper to invoke a registered tool via the FastMCP machinery.

    FastMCP may return either:
      - a 2-tuple (content_blocks, structured_dict) when the tool function has
        a typed return that produces structured output, or
      - a bare list of content blocks otherwise.

    In both cases we want the Python-native return value. When structured is
    available we use it; otherwise we parse the text block as JSON.
    """
    import json as _json

    result = await server.call_tool(name, kwargs)

    structured: dict | None = None
    blocks = result
    if isinstance(result, tuple) and len(result) == 2:
        blocks, structured = result

    if isinstance(structured, dict):
        # FastMCP wraps non-dict returns as {"result": value}; unwrap those.
        if set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    # Fall back to parsing the first text block as JSON.
    if blocks:
        text = getattr(blocks[0], "text", None)
        if text is not None:
            try:
                return _json.loads(text)
            except _json.JSONDecodeError:
                return text
    return None


@pytest.fixture
def call(server):
    """Async helper bound to the test server."""
    async def _bound(name: str, **kwargs):
        return await _call(server, name, **kwargs)
    return _bound
