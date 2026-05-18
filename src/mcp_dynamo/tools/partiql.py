"""PartiQL surface: single statement and batch.

DynamoDB PartiQL is SQL-flavored but heavily constrained:
- non-scan SELECTs must include the partition key in WHERE
- no JOINs, no subqueries
- string literals are single-quoted
- parameter placeholders are ``?`` (positional)

We expose both ``execute_statement`` and ``batch_execute_statement`` and use
the resource-level serializer so parameters can be Python-native dicts.
"""

from __future__ import annotations

from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError
from mcp.server.fastmcp import FastMCP

from mcp_dynamo.client import get_dynamodb_client
from mcp_dynamo.errors import interpret_dynamo_error
from mcp_dynamo.formatting import render
from mcp_dynamo.safety import requires_confirm, statement_is_destructive

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _serialize_params(params: list[Any] | None) -> list[dict[str, Any]] | None:
    if not params:
        return None
    return [_serializer.serialize(p) for p in params]


def _deserialize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        out.append({k: _deserializer.deserialize(v) for k, v in item.items()})
    return out


def _statement_destructive(args: dict[str, Any]) -> bool:
    return statement_is_destructive(args.get("statement", ""))


def _batch_destructive(args: dict[str, Any]) -> bool:
    statements = args.get("statements") or []
    return any(
        isinstance(s, dict) and statement_is_destructive(s.get("Statement", ""))
        for s in statements
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    @requires_confirm(
        action="execute_partiql_statement",
        target_keys=("statement", "parameters"),
        is_destructive=_statement_destructive,
    )
    def execute_partiql_statement(
        statement: str,
        parameters: list[Any] | None = None,
        consistent_read: bool = False,
        next_token: str | None = None,
        limit: int | None = None,
        format: str = "json",
        confirm: bool = False,
    ) -> Any:
        """Run a single PartiQL statement against DynamoDB.

        Supports ``SELECT``, ``INSERT``, ``UPDATE``, ``DELETE``. DELETE
        statements require ``confirm=true`` or a dry-run preview is returned.

        Parameter placeholders are positional ``?`` and substituted from
        ``parameters`` (Python-native; we serialize to DynamoDB types
        internally).

        Notes
        -----
        - For SELECT, the partition key must appear in the WHERE clause
          (otherwise DynamoDB does a scan — slow and expensive).
        - String literals use single quotes: ``WHERE id = 'u_123'``.
        - No JOINs.

        Examples
        --------
        ``execute_partiql_statement(statement="SELECT * FROM Users WHERE id = ?",
                                    parameters=["u_123"])``

        ``execute_partiql_statement(statement="DELETE FROM Users WHERE id = ?",
                                    parameters=["u_old"], confirm=true)``
        """
        client = get_dynamodb_client()
        kwargs: dict[str, Any] = {"Statement": statement, "ConsistentRead": consistent_read}
        serialized = _serialize_params(parameters)
        if serialized is not None:
            kwargs["Parameters"] = serialized
        if next_token:
            kwargs["NextToken"] = next_token
        if limit is not None:
            kwargs["Limit"] = limit

        try:
            resp = client.execute_statement(**kwargs)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        items = _deserialize_items(resp.get("Items", []))
        payload: dict[str, Any] = {
            "items": items,
            "count": len(items),
            "next_token": resp.get("NextToken"),
        }
        return render(payload, format, rows_key="items", title="PartiQL result")

    @mcp.tool()
    @requires_confirm(
        action="execute_partiql_batch",
        target_keys=("statements",),
        is_destructive=_batch_destructive,
    )
    def execute_partiql_batch(
        statements: list[dict[str, Any]],
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Run up to 25 PartiQL statements in a single call.

        ``statements`` is a list of ``{"Statement": str, "Parameters": [...]}``
        objects (parameters optional). If any statement is a ``DELETE``,
        ``confirm=true`` is required.

        Returns the raw per-statement responses (deserialized).

        Example
        -------
        ``execute_partiql_batch(statements=[
            {"Statement": "INSERT INTO Users VALUE {'id': ?, 'name': ?}",
             "Parameters": ["u_1", "Ada"]},
            {"Statement": "INSERT INTO Users VALUE {'id': ?, 'name': ?}",
             "Parameters": ["u_2", "Grace"]}
        ])``
        """
        client = get_dynamodb_client()
        prepared: list[dict[str, Any]] = []
        for stmt in statements:
            entry: dict[str, Any] = {"Statement": stmt["Statement"]}
            params = stmt.get("Parameters")
            serialized = _serialize_params(params)
            if serialized is not None:
                entry["Parameters"] = serialized
            if "ConsistentRead" in stmt:
                entry["ConsistentRead"] = stmt["ConsistentRead"]
            prepared.append(entry)

        try:
            resp = client.batch_execute_statement(Statements=prepared)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        responses: list[dict[str, Any]] = []
        for r in resp.get("Responses", []):
            entry: dict[str, Any] = {}
            if "Error" in r:
                entry["error"] = r["Error"]
            if "Item" in r:
                entry["item"] = {k: _deserializer.deserialize(v) for k, v in r["Item"].items()}
            if "TableName" in r:
                entry["table_name"] = r["TableName"]
            responses.append(entry)
        return {"ok": True, "responses": responses}


__all__ = ["register"]
