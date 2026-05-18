"""Table lifecycle: create, update, delete.

Thin wrappers over the low-level client. The shapes intentionally mirror the
boto3 names so the LLM can paste in standard DynamoDB JSON.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from mcp.server.fastmcp import FastMCP

from mcp_dynamo.access_control import check_table_access
from mcp_dynamo.client import get_config, get_dynamodb_client
from mcp_dynamo.errors import interpret_dynamo_error
from mcp_dynamo.formatting import to_json
from mcp_dynamo.safety import requires_confirm


def _gsi_has_deletes(args: dict[str, Any]) -> bool:
    """Return True if any GSI update entry is a Delete."""
    updates = args.get("global_secondary_index_updates") or []
    return any(isinstance(u, dict) and "Delete" in u for u in updates)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def create_table(
        table_name: str,
        key_schema: list[dict[str, str]],
        attribute_definitions: list[dict[str, str]],
        billing_mode: str = "PAY_PER_REQUEST",
        provisioned_throughput: dict[str, int] | None = None,
        global_secondary_indexes: list[dict[str, Any]] | None = None,
        local_secondary_indexes: list[dict[str, Any]] | None = None,
        stream_specification: dict[str, Any] | None = None,
        tags: list[dict[str, str]] | None = None,
        wait_until_active: bool = True,
    ) -> dict[str, Any]:
        """Create a new DynamoDB table.

        Parameters
        ----------
        key_schema:
            e.g. ``[{"AttributeName": "id", "KeyType": "HASH"}]`` (and optionally
            a ``RANGE`` entry for composite keys).
        attribute_definitions:
            One ``{"AttributeName": ..., "AttributeType": "S|N|B"}`` per key
            attribute (including GSI/LSI keys).
        billing_mode:
            ``"PAY_PER_REQUEST"`` (default) or ``"PROVISIONED"``. If provisioned,
            also pass ``provisioned_throughput``.
        global_secondary_indexes / local_secondary_indexes:
            Standard DynamoDB index specs.
        wait_until_active:
            If True (default), block until the table reports ``ACTIVE`` before
            returning.

        Example
        -------
        ``create_table(
            table_name="Tasks",
            key_schema=[{"AttributeName": "id", "KeyType": "HASH"}],
            attribute_definitions=[{"AttributeName": "id", "AttributeType": "S"}],
        )``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        client = get_dynamodb_client()
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "KeySchema": key_schema,
            "AttributeDefinitions": attribute_definitions,
        }
        if billing_mode == "PROVISIONED":
            if not provisioned_throughput:
                raise ValueError("provisioned_throughput is required when billing_mode='PROVISIONED'")
            kwargs["BillingMode"] = "PROVISIONED"
            kwargs["ProvisionedThroughput"] = provisioned_throughput
        else:
            kwargs["BillingMode"] = billing_mode
        if global_secondary_indexes:
            kwargs["GlobalSecondaryIndexes"] = global_secondary_indexes
        if local_secondary_indexes:
            kwargs["LocalSecondaryIndexes"] = local_secondary_indexes
        if stream_specification:
            kwargs["StreamSpecification"] = stream_specification
        if tags:
            kwargs["Tags"] = tags

        try:
            resp = client.create_table(**kwargs)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        description = resp.get("TableDescription", {})

        if wait_until_active:
            try:
                waiter = client.get_waiter("table_exists")
                waiter.wait(TableName=table_name)
                description = client.describe_table(TableName=table_name).get("Table", description)
            except ClientError as exc:
                return {
                    "ok": False,
                    "table": to_json(description),
                    "wait_error": exc.response.get("Error", {}).get("Message", str(exc)),
                }

        return {"ok": True, "table": to_json(description)}

    @mcp.tool()
    @requires_confirm(
        action="update_table",
        target_keys=("table_name", "global_secondary_index_updates"),
        is_destructive=_gsi_has_deletes,
    )
    def update_table(
        table_name: str,
        attribute_definitions: list[dict[str, str]] | None = None,
        billing_mode: str | None = None,
        provisioned_throughput: dict[str, int] | None = None,
        global_secondary_index_updates: list[dict[str, Any]] | None = None,
        stream_specification: dict[str, Any] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Modify an existing table: change billing mode, throughput, GSIs, streams.

        Pass only the fields you want to change. Note: GSI updates use the
        ``GlobalSecondaryIndexUpdates`` shape (``Create`` / ``Update`` /
        ``Delete`` entries), not the create-table shape.

        If ``global_secondary_index_updates`` contains a ``Delete`` entry,
        ``confirm=true`` is required — GSI deletion is irreversible.

        Example
        -------
        ``update_table(table_name="Users", billing_mode="PAY_PER_REQUEST")``

        GSI delete (requires confirm):
        ``update_table(table_name="Users",
                       global_secondary_index_updates=[{"Delete": {"IndexName": "old-idx"}}],
                       confirm=true)``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        client = get_dynamodb_client()
        kwargs: dict[str, Any] = {"TableName": table_name}
        if attribute_definitions:
            kwargs["AttributeDefinitions"] = attribute_definitions
        if billing_mode:
            kwargs["BillingMode"] = billing_mode
        if provisioned_throughput:
            kwargs["ProvisionedThroughput"] = provisioned_throughput
        if global_secondary_index_updates:
            kwargs["GlobalSecondaryIndexUpdates"] = global_secondary_index_updates
        if stream_specification:
            kwargs["StreamSpecification"] = stream_specification

        if len(kwargs) == 1:
            raise ValueError("update_table requires at least one field to change.")

        try:
            resp = client.update_table(**kwargs)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        return {"ok": True, "table": to_json(resp.get("TableDescription"))}

    @mcp.tool()
    @requires_confirm(action="delete_table", target_keys=("table_name",))
    def delete_table(
        table_name: str,
        wait_until_deleted: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Delete a table. Requires ``confirm=true``.

        Without ``confirm=true`` this returns a dry-run preview. With it, the
        table and all of its data are gone — no undo. Defaults to waiting until
        the table is fully deleted before returning.

        Example
        -------
        Preview: ``delete_table(table_name="Tasks")``
        Execute: ``delete_table(table_name="Tasks", confirm=true)``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        client = get_dynamodb_client()
        try:
            resp = client.delete_table(TableName=table_name)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        description = resp.get("TableDescription", {})

        if wait_until_deleted:
            try:
                waiter = client.get_waiter("table_not_exists")
                waiter.wait(TableName=table_name)
            except ClientError as exc:
                return {
                    "ok": False,
                    "table": to_json(description),
                    "wait_error": exc.response.get("Error", {}).get("Message", str(exc)),
                }

        return {"ok": True, "table": to_json(description)}


__all__ = ["register"]
