"""Single-item CRUD and batch operations.

These tools use the high-level boto3 Resource API (``Table``) so callers can
pass Python-native dicts for keys/items rather than DynamoDB AttributeValue
maps. Batch operations stay on the low-level resource where they're clearer.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from botocore.exceptions import ClientError
from mcp.server.fastmcp import FastMCP

from mcp_dynamo.access_control import check_table_access
from mcp_dynamo.client import get_config, get_dynamodb_client, get_dynamodb_resource
from mcp_dynamo.errors import interpret_dynamo_error
from mcp_dynamo.formatting import to_json, to_table
from mcp_dynamo.safety import requires_confirm

ReturnValuesLiteral = Literal["NONE", "ALL_OLD", "UPDATED_OLD", "ALL_NEW", "UPDATED_NEW"]
PutReturnValuesLiteral = Literal["NONE", "ALL_OLD"]
DeleteReturnValuesLiteral = Literal["NONE", "ALL_OLD"]

# Chunk caps imposed by DynamoDB's API limits.
BATCH_WRITE_CHUNK = 25
BATCH_GET_CHUNK = 100

_REMOVE_PATTERN = re.compile(r"\bREMOVE\b", re.IGNORECASE)


def _expression_kwargs(
    *,
    condition: str | None = None,
    names: dict[str, str] | None = None,
    values: dict[str, Any] | None = None,
    update: str | None = None,
    projection: str | None = None,
    filter: str | None = None,
    key_condition: str | None = None,
) -> dict[str, Any]:
    """Build the optional ``ExpressionAttribute*`` kwargs DynamoDB expects.

    Centralizes the repetitive "if foo: kwargs['Foo'] = foo" pattern that
    appeared in every write tool. Only includes keys that were supplied.
    """
    out: dict[str, Any] = {}
    if condition:
        out["ConditionExpression"] = condition
    if names:
        out["ExpressionAttributeNames"] = names
    if values:
        out["ExpressionAttributeValues"] = values
    if update:
        out["UpdateExpression"] = update
    if projection:
        out["ProjectionExpression"] = projection
    if filter:
        out["FilterExpression"] = filter
    if key_condition:
        out["KeyConditionExpression"] = key_condition
    return out


def _update_has_remove(args: dict[str, Any]) -> bool:
    """Return True if the UpdateExpression contains a REMOVE clause.

    REMOVE deletes attributes from an existing item — semantically destructive,
    so it must go through the confirm gate. Apply NFKC normalization first to
    defeat zero-width-character obfuscation (consistent with safety.py).
    """
    expr = args.get("update_expression") or ""
    norm = "".join(
        c for c in unicodedata.normalize("NFKC", expr)
        if unicodedata.category(c) != "Cf"
    )
    return bool(_REMOVE_PATTERN.search(norm))


def _validate_request_items_shape(request_items: dict[str, Any]) -> None:
    """Strict validation of a ``batch_write_item`` request_items dict.

    Each entry must be a single-key dict with either ``PutRequest`` or
    ``DeleteRequest``. Anything else raises ``ValueError`` with the offending
    table + index so the LLM can fix its call.
    """
    if not isinstance(request_items, dict):
        raise ValueError("request_items must be a dict of table-name → list[request]")
    for tbl, reqs in request_items.items():
        if not isinstance(reqs, list):
            raise ValueError(f"request_items[{tbl!r}] must be a list of requests")
        for i, req in enumerate(reqs):
            if not isinstance(req, dict):
                raise ValueError(f"invalid request_items shape at table {tbl!r}, index {i}")
            keys = set(req.keys())
            if keys not in ({"PutRequest"}, {"DeleteRequest"}):
                raise ValueError(f"invalid request_items shape at table {tbl!r}, index {i}")


def _batch_write_has_deletes(args: dict[str, Any]) -> bool:
    items = args.get("request_items") or {}
    if not isinstance(items, dict):
        return False
    for reqs in items.values():
        if not isinstance(reqs, list):
            continue
        for req in reqs:
            if isinstance(req, dict) and "DeleteRequest" in req:
                return True
    return False


def _chunk(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_item(
        table_name: str,
        key: dict[str, Any],
        format: Literal["json", "table"] = "json",
    ) -> dict[str, Any]:
        """Fetch a single item by primary key.

        ``key`` must include the partition key (and sort key, for composite
        tables) using Python-native types. Returns
        ``{"item": ..., "rendered": str | None}`` (``item`` is ``None`` if not
        found).

        Example
        -------
        ``get_item(table_name="Users", key={"id": "u_123"})``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        resource = get_dynamodb_resource()
        table = resource.Table(table_name)
        try:
            resp = table.get_item(Key=key)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        item = resp.get("Item")
        rendered: str | None = None
        if format == "table" and item is not None:
            rendered = to_table([to_json(item)], title=f"{table_name} item")
        return {"item": to_json(item) if item is not None else None, "rendered": rendered}

    @mcp.tool()
    def put_item(
        table_name: str,
        item: dict[str, Any],
        condition_expression: str | None = None,
        expression_attribute_names: dict[str, str] | None = None,
        expression_attribute_values: dict[str, Any] | None = None,
        return_values: PutReturnValuesLiteral = "NONE",
    ) -> dict[str, Any]:
        """Insert or replace an item. By default an existing item at the same key is overwritten.

        Pass ``condition_expression`` (e.g. ``"attribute_not_exists(id)"``) to
        make the put fail when the key already exists. Use
        ``expression_attribute_names`` / ``expression_attribute_values`` to
        substitute reserved names / values in the condition.

        ``return_values`` is one of ``"NONE"`` (default) or ``"ALL_OLD"``.

        Example
        -------
        ``put_item(table_name="Users", item={"id": "u_123", "name": "Ada"},
                   condition_expression="attribute_not_exists(id)")``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        resource = get_dynamodb_resource()
        table = resource.Table(table_name)
        kwargs: dict[str, Any] = {"Item": item, "ReturnValues": return_values}
        kwargs.update(
            _expression_kwargs(
                condition=condition_expression,
                names=expression_attribute_names,
                values=expression_attribute_values,
            )
        )
        try:
            resp = table.put_item(**kwargs)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        result: dict[str, Any] = {"ok": True, "table": table_name}
        attrs = resp.get("Attributes")
        if attrs is not None:
            result["attributes"] = to_json(attrs)
        return result

    @mcp.tool()
    @requires_confirm(
        action="update_item",
        target_keys=("table_name", "key", "update_expression"),
        is_destructive=_update_has_remove,
    )
    def update_item(
        table_name: str,
        key: dict[str, Any],
        update_expression: str,
        expression_attribute_names: dict[str, str] | None = None,
        expression_attribute_values: dict[str, Any] | None = None,
        condition_expression: str | None = None,
        return_values: ReturnValuesLiteral = "ALL_NEW",
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Patch an item using an ``UpdateExpression``.

        Parameters
        ----------
        update_expression:
            Standard DynamoDB UpdateExpression, e.g.
            ``"SET #n = :name, version = version + :one"``. If it contains a
            ``REMOVE`` clause, ``confirm=true`` is required because REMOVE
            deletes attributes from an existing item.
        expression_attribute_names / expression_attribute_values:
            Placeholder substitutions used in ``update_expression`` /
            ``condition_expression``.
        return_values:
            One of ``NONE``, ``ALL_OLD``, ``UPDATED_OLD``, ``ALL_NEW``,
            ``UPDATED_NEW``. Default ``ALL_NEW``.

        Example
        -------
        ``update_item(
            table_name="Users", key={"id": "u_123"},
            update_expression="SET #n = :name",
            expression_attribute_names={"#n": "name"},
            expression_attribute_values={":name": "Ada Lovelace"},
        )``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        resource = get_dynamodb_resource()
        table = resource.Table(table_name)
        kwargs: dict[str, Any] = {
            "Key": key,
            "ReturnValues": return_values,
        }
        kwargs.update(
            _expression_kwargs(
                condition=condition_expression,
                names=expression_attribute_names,
                values=expression_attribute_values,
                update=update_expression,
            )
        )
        try:
            resp = table.update_item(**kwargs)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        return {"ok": True, "attributes": to_json(resp.get("Attributes"))}

    @mcp.tool()
    @requires_confirm(action="delete_item", target_keys=("table_name", "key"))
    def delete_item(
        table_name: str,
        key: dict[str, Any],
        condition_expression: str | None = None,
        expression_attribute_names: dict[str, str] | None = None,
        expression_attribute_values: dict[str, Any] | None = None,
        return_values: DeleteReturnValuesLiteral = "ALL_OLD",
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Delete an item by primary key. Requires ``confirm=true``.

        Without ``confirm=true`` the tool returns a dry-run preview describing
        what would be deleted. Call again with ``confirm=true`` to actually
        delete.

        ``return_values`` is one of ``"NONE"`` or ``"ALL_OLD"`` (default).

        Example
        -------
        Preview: ``delete_item(table_name="Users", key={"id": "u_123"})``
        Execute: ``delete_item(table_name="Users", key={"id": "u_123"}, confirm=true)``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        resource = get_dynamodb_resource()
        table = resource.Table(table_name)
        kwargs: dict[str, Any] = {"Key": key, "ReturnValues": return_values}
        kwargs.update(
            _expression_kwargs(
                condition=condition_expression,
                names=expression_attribute_names,
                values=expression_attribute_values,
            )
        )
        try:
            resp = table.delete_item(**kwargs)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        return {"ok": True, "deleted": to_json(resp.get("Attributes"))}

    @mcp.tool()
    def batch_get_item(
        request_items: dict[str, dict[str, Any]],
        format: Literal["json", "table"] = "json",
    ) -> dict[str, Any]:
        """Fetch multiple items across one or more tables in a single call.

        ``request_items`` shape::

            {
                "Users": {"Keys": [{"id": "u_1"}, {"id": "u_2"}]},
                "Orders": {"Keys": [{"user_id": "u_1", "order_id": "o_9"}]}
            }

        Per-table options (``ConsistentRead``, ``ProjectionExpression``,
        ``ExpressionAttributeNames``) are forwarded as-is.

        If any table has more than 100 keys, the call is auto-chunked into
        multiple boto3 ``batch_get_item`` calls and the responses are merged.

        Example
        -------
        ``batch_get_item(request_items={"Users": {"Keys": [{"id": "u_1"}]}})``
        """
        # Access control: check every table in the request before any AWS call.
        cfg = get_config()
        for tbl in request_items:
            if denied := check_table_access(tbl, cfg):
                return denied

        # Use the low-level client to retain the explicit shape and chunk it.
        # The high-level resource auto-converts but doesn't auto-chunk.
        client = get_dynamodb_client()
        resource = get_dynamodb_resource()

        # Pre-serialize: easier to chunk by raw Keys. We send chunks via the
        # high-level resource so callers' Python-native key dicts work.
        merged_responses: dict[str, list[dict[str, Any]]] = {}
        merged_unprocessed: dict[str, dict[str, Any]] = {}

        # Build per-table chunk plans. For each table, split Keys into <=100 chunks
        # but always carry the table's other options (ConsistentRead, etc.)
        # alongside each chunk.
        # We assemble one boto3 call per chunk-index across all tables so we
        # match DynamoDB's single-call shape — but if a single table exceeds the
        # max we just send multiple sequential calls.
        table_names = list(request_items.keys())
        max_chunks = 1
        per_table_chunks: dict[str, list[dict[str, Any]]] = {}
        for tbl in table_names:
            spec = request_items[tbl]
            keys = spec.get("Keys", [])
            chunked_keys = _chunk(keys, BATCH_GET_CHUNK) if len(keys) > BATCH_GET_CHUNK else [keys]
            per_table_chunks[tbl] = [
                {**{k: v for k, v in spec.items() if k != "Keys"}, "Keys": chunk}
                for chunk in chunked_keys
            ]
            max_chunks = max(max_chunks, len(per_table_chunks[tbl]))

        for chunk_index in range(max_chunks):
            this_call: dict[str, dict[str, Any]] = {}
            for tbl in table_names:
                chunks = per_table_chunks[tbl]
                if chunk_index < len(chunks):
                    this_call[tbl] = chunks[chunk_index]
            if not this_call:
                continue
            try:
                resp = resource.batch_get_item(RequestItems=this_call)
            except ClientError as exc:
                return interpret_dynamo_error(exc)
            for tbl, rows in resp.get("Responses", {}).items():
                merged_responses.setdefault(tbl, []).extend(rows)
            for tbl, spec in resp.get("UnprocessedKeys", {}).items():
                # Merge by extending Keys lists; preserve other per-table opts.
                if tbl in merged_unprocessed:
                    existing = merged_unprocessed[tbl].setdefault("Keys", [])
                    existing.extend(spec.get("Keys", []))
                else:
                    merged_unprocessed[tbl] = spec
            # Touch `client` so type stubs treat it as used; we use the resource
            # for the actual call but want both available for future tuning.
            _ = client

        responses = to_json(merged_responses)
        unprocessed = to_json(merged_unprocessed)
        rendered: str | None = None
        if format == "table":
            rendered_blocks: list[str] = []
            for tbl, rows in responses.items():
                if rows:
                    rendered_blocks.append(to_table(rows, title=tbl))
            rendered = "\n\n".join(rendered_blocks) if rendered_blocks else "(no rows)"
        return {
            "responses": responses,
            "unprocessed_keys": unprocessed,
            "rendered": rendered,
        }

    @mcp.tool()
    @requires_confirm(
        action="batch_write_item",
        target_keys=("request_items",),
        is_destructive=_batch_write_has_deletes,
    )
    def batch_write_item(
        request_items: dict[str, list[dict[str, Any]]],
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Bulk put and/or delete across one or more tables.

        ``request_items`` is the standard DynamoDB shape::

            {
                "Users": [
                    {"PutRequest": {"Item": {"id": "u_1", "name": "Ada"}}},
                    {"DeleteRequest": {"Key": {"id": "u_old"}}}
                ]
            }

        If any ``DeleteRequest`` is present, ``confirm=true`` is required;
        pure-put batches run immediately. If any table has more than 25
        requests, the call is auto-chunked into multiple boto3
        ``batch_write_item`` calls. Returns the merged ``UnprocessedItems``
        across all chunks so the caller can retry partial failures.

        Example
        -------
        ``batch_write_item(request_items={"Users": [{"PutRequest": {"Item": {"id": "u_1"}}}]})``
        """
        _validate_request_items_shape(request_items)
        # Access control: check every table in the request before any AWS call.
        cfg = get_config()
        for tbl in request_items:
            if denied := check_table_access(tbl, cfg):
                return denied
        resource = get_dynamodb_resource()

        table_names = list(request_items.keys())
        per_table_chunks: dict[str, list[list[dict[str, Any]]]] = {
            tbl: (
                _chunk(request_items[tbl], BATCH_WRITE_CHUNK)
                if len(request_items[tbl]) > BATCH_WRITE_CHUNK
                else [request_items[tbl]]
            )
            for tbl in table_names
        }
        max_chunks = max((len(chunks) for chunks in per_table_chunks.values()), default=1)

        merged_unprocessed: dict[str, list[dict[str, Any]]] = {}
        for chunk_index in range(max_chunks):
            call_items: dict[str, list[dict[str, Any]]] = {}
            for tbl in table_names:
                chunks = per_table_chunks[tbl]
                if chunk_index < len(chunks):
                    call_items[tbl] = chunks[chunk_index]
            if not call_items:
                continue
            try:
                resp = resource.batch_write_item(RequestItems=call_items)
            except ClientError as exc:
                return interpret_dynamo_error(exc)
            for tbl, reqs in resp.get("UnprocessedItems", {}).items():
                merged_unprocessed.setdefault(tbl, []).extend(reqs)

        return {
            "ok": True,
            "unprocessed_items": to_json(merged_unprocessed),
        }


__all__ = ["register"]
