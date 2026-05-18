"""Tests for the error interpretation module.

Unit tests use manual exception construction (no moto needed) to verify each
mapped error code. An integration test triggers a real ResourceNotFoundException
via moto to confirm the wiring end-to-end.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from mcp_dynamo.errors import interpret_dynamo_error


def _make_client_error(code: str, message: str = "some message", http_status: int = 400) -> ClientError:
    """Construct a synthetic ClientError matching the boto3 shape."""
    response = {
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": http_status},
    }
    return ClientError(response, "TestOperation")


# ---------------------------------------------------------------------------
# Unit tests — one per mapped error code
# ---------------------------------------------------------------------------


def test_conditional_check_failed():
    exc = _make_client_error("ConditionalCheckFailedException")
    result = interpret_dynamo_error(exc)
    assert result["error"] == "ConditionalCheckFailedException"
    assert "condition expression" in result["message"]
    assert "ConditionExpression" in result["message"]


def test_provisioned_throughput_exceeded():
    exc = _make_client_error("ProvisionedThroughputExceededException")
    result = interpret_dynamo_error(exc)
    assert result["error"] == "ProvisionedThroughputExceededException"
    assert "capacity" in result["message"].lower()
    assert "on-demand" in result["message"]


def test_resource_not_found():
    exc = _make_client_error("ResourceNotFoundException", http_status=404)
    result = interpret_dynamo_error(exc)
    assert result["error"] == "ResourceNotFoundException"
    assert "not exist" in result["message"]


def test_validation_exception():
    exc = _make_client_error("ValidationException", message="Invalid expression: syntax error at token")
    result = interpret_dynamo_error(exc)
    assert result["error"] == "ValidationException"
    assert "syntax error at token" in result["message"]
    assert "expression syntax" in result["message"]


def test_transaction_conflict():
    exc = _make_client_error("TransactionConflictException")
    result = interpret_dynamo_error(exc)
    assert result["error"] == "TransactionConflictException"
    assert "transaction" in result["message"].lower()
    assert "retry" in result["message"].lower()


def test_item_collection_size_limit():
    exc = _make_client_error("ItemCollectionSizeLimitExceededException")
    result = interpret_dynamo_error(exc)
    assert result["error"] == "ItemCollectionSizeLimitExceededException"
    assert "10 GB" in result["message"]


def test_request_limit_exceeded():
    exc = _make_client_error("RequestLimitExceeded", http_status=400)
    result = interpret_dynamo_error(exc)
    assert result["error"] == "RequestLimitExceeded"
    assert "rate limit" in result["message"].lower()
    assert "exponential backoff" in result["message"]


def test_unknown_error_code_includes_code_and_status():
    exc = _make_client_error("SomeObscureErrorCode", message="something went wrong", http_status=500)
    result = interpret_dynamo_error(exc)
    assert result["error"] == "SomeObscureErrorCode"
    # Generic wrapper must include code + HTTP status for debuggability
    assert "SomeObscureErrorCode" in result["message"]
    assert "500" in result["message"]
    assert "something went wrong" in result["message"]


def test_unknown_error_without_http_status():
    """If ResponseMetadata is missing, we should still produce a safe response."""
    response = {"Error": {"Code": "WeirdError", "Message": "details"}}
    exc = ClientError(response, "Op")
    result = interpret_dynamo_error(exc)
    assert result["error"] == "WeirdError"
    assert "details" in result["message"]


# ---------------------------------------------------------------------------
# Integration test — trigger real ResourceNotFoundException via moto
# ---------------------------------------------------------------------------


async def test_get_item_nonexistent_table_returns_error_dict(call, aws):
    """Calling get_item on a table that doesn't exist should return an interpreted error."""
    # No seed_tables fixture here — we want the table to be missing.
    result = await call("get_item", table_name="NoSuchTable", key={"id": "x"})
    assert isinstance(result, dict)
    assert result.get("error") == "ResourceNotFoundException"
    assert "not exist" in result["message"]


async def test_query_nonexistent_table_returns_error_dict(call, aws):
    result = await call(
        "query",
        table_name="NoSuchTable",
        key_condition_expression="id = :v",
        expression_attribute_values={":v": "x"},
    )
    assert isinstance(result, dict)
    assert result.get("error") == "ResourceNotFoundException"


async def test_scan_nonexistent_table_returns_error_dict(call, aws):
    result = await call("scan", table_name="NoSuchTable")
    assert isinstance(result, dict)
    assert result.get("error") == "ResourceNotFoundException"
