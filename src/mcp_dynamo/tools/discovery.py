"""Read-only discovery tools: identity, list, describe."""

from __future__ import annotations

from typing import Any, Literal

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)
from mcp.server.fastmcp import FastMCP

from mcp_dynamo.access_control import check_table_access
from mcp_dynamo.client import get_config, get_dynamodb_client, get_sts_client
from mcp_dynamo.errors import interpret_dynamo_error
from mcp_dynamo.formatting import to_json, to_table


def _redact_arn(arn: str | None) -> str | None:
    """Redact the 12-digit Account ID from an STS caller-identity Arn.

    Returns the Arn with the account number replaced by the last 4 digits
    (e.g. ``arn:aws:iam::****1234:user/alice``). We avoid surfacing the full
    Account ID because tool responses flow into the LLM transcript and
    eventually into logs / training data.
    """
    if not arn:
        return arn
    parts = arn.split(":")
    # ARN shape: arn:aws:service:region:account:resource
    if len(parts) >= 6 and parts[4].isdigit() and len(parts[4]) == 12:
        parts[4] = f"****{parts[4][-4:]}"
        return ":".join(parts)
    return arn


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def dynamo_whoami() -> dict[str, Any]:
        """Return the active AWS identity, region, and DynamoDB endpoint.

        Useful for confirming which account / role / DynamoDB Local instance the
        server is talking to before running destructive operations. Calls STS
        ``GetCallerIdentity``; if STS is unreachable (e.g. DynamoDB Local with
        no real creds) the identity section is omitted.

        The returned Arn has the 12-digit Account ID redacted to ``****NNNN``
        so the LLM transcript does not carry a full AWS account number.

        Example
        -------
        Call with no arguments. Returns::

            {
                "region": "us-east-1",
                "endpoint_url": "http://localhost:8000",
                "is_local": true,
                "has_endpoint_override": true,
                "identity": {"UserId": "...", "Arn": "arn:aws:iam::****6789:..."}
            }
        """
        cfg = get_config()
        result: dict[str, Any] = {
            "region": cfg.region,
            "endpoint_url": cfg.endpoint_url,
            "is_local": cfg.is_local,
            "has_endpoint_override": cfg.has_endpoint_override,
        }
        try:
            sts = get_sts_client()
            ident = sts.get_caller_identity()
            result["identity"] = {
                "UserId": ident.get("UserId"),
                "Arn": _redact_arn(ident.get("Arn")),
            }
        except (ClientError, BotoCoreError, NoCredentialsError, EndpointConnectionError) as exc:
            if isinstance(exc, ClientError):
                result["identity_error"] = exc.response.get("Error", {}).get("Message", str(exc))
            else:
                result["identity_error"] = str(exc)
        return result

    @mcp.tool()
    def list_tables(
        prefix: str | None = None,
        limit: int = 100,
        format: Literal["json", "table"] = "json",
    ) -> dict[str, Any]:
        """List DynamoDB table names in the active region.

        Parameters
        ----------
        prefix:
            Optional case-sensitive prefix filter applied client-side after the
            paginator yields names.
        limit:
            Maximum number of names to return (after prefix filtering). Default 100.
        format:
            ``"json"`` (default) or ``"table"``.

        Returns
        -------
        ``{"tables": [...], "count": N, "rendered": str | None}``. ``rendered``
        is a string when ``format="table"`` and ``None`` otherwise.

        Example
        -------
        ``list_tables(prefix="prod_")`` →
        ``{"tables": ["prod_users", "prod_orders"], "count": 2, "rendered": None}``
        """
        client = get_dynamodb_client()
        paginator = client.get_paginator("list_tables")
        # Cap pagination so we never buffer millions of table names.
        # Without prefix: cap directly at `limit`.
        # With prefix: filtering is client-side so we need more names, but
        # we still cap at a safe ceiling (10 000) to prevent unbounded buffering.
        page_cap = limit if not prefix else 10_000
        iterator = paginator.paginate(PaginationConfig={"MaxItems": page_cap})

        names: list[str] = []
        try:
            for page in iterator:
                names.extend(page.get("TableNames", []))
        except (ClientError, BotoCoreError, EndpointConnectionError, NoCredentialsError):
            raise

        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        # Filter out tables that are denied by access control so the LLM
        # never sees table names it can't actually use.
        cfg = get_config()
        names = [n for n in names if check_table_access(n, cfg) is None]
        names = names[:limit]

        rendered = (
            to_table([{"name": n} for n in names], title="Tables")
            if format == "table"
            else None
        )
        return {"tables": names, "count": len(names), "rendered": rendered}

    @mcp.tool()
    def describe_table(
        table_name: str,
        format: Literal["json", "table"] = "json",
    ) -> dict[str, Any]:
        """Describe a table: schema, indexes, throughput, item count.

        Returns the full DynamoDB ``DescribeTable`` payload (minus volatile
        ``ResponseMetadata``). Use this before writing a ``query`` to confirm
        partition/sort key names and any GSIs available.

        Example
        -------
        ``describe_table(table_name="Users")``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        client = get_dynamodb_client()
        try:
            resp = client.describe_table(TableName=table_name)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        except (BotoCoreError, EndpointConnectionError, NoCredentialsError):
            raise
        table = resp.get("Table", {})
        payload = to_json(table)
        rendered: str | None = None
        if format == "table":
            summary = [{
                "TableName": table.get("TableName"),
                "Status": table.get("TableStatus"),
                "ItemCount": table.get("ItemCount"),
                "KeySchema": table.get("KeySchema"),
                "GSIs": [g.get("IndexName") for g in table.get("GlobalSecondaryIndexes", [])],
                "LSIs": [g.get("IndexName") for g in table.get("LocalSecondaryIndexes", [])],
            }]
            rendered = to_table(summary, title=f"Table: {table_name}")
        # Spread the table payload at the top level (matches the previous shape)
        # and attach rendered alongside.
        result: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {"table": payload}
        result["rendered"] = rendered
        return result

    @mcp.tool()
    def infer_schema(
        table_name: str,
        sample_size: int = 100,
        format: Literal["json", "table"] = "json",
    ) -> dict[str, Any]:
        """Sample items from a table and infer the de-facto attribute schema.

        Calls ``DescribeTable`` to get key schema and GSI definitions, then
        ``Scan`` with ``Limit=sample_size`` (single page) to collect a sample.
        For each attribute observed in the sample, returns:

        - ``type`` — the DynamoDB type key (``S``, ``N``, ``BOOL``, ``L``,
          ``M``, ``SS``, ``NS``, ``BS``, ``NULL``).
        - ``present_in`` — percentage of sampled items that contained the
          attribute, rounded to the nearest integer.
        - ``sample_values`` — included only for string (``S``) attributes with
          10 or fewer distinct values across the sample.

        GSI hints are formatted as human-readable strings describing useful
        access patterns.

        Respects the allow/block list: returns ``TableAccessDenied`` if the
        table is not accessible.

        Parameters
        ----------
        table_name:
            Name of the table to inspect.
        sample_size:
            Maximum number of items to scan. Default 100.
        format:
            ``"json"`` (default) or ``"table"``.

        Example
        -------
        ``infer_schema(table_name="Orders")``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied

        client = get_dynamodb_client()

        # --- DescribeTable for key schema and GSI hints ---
        try:
            desc_resp = client.describe_table(TableName=table_name)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        except (BotoCoreError, EndpointConnectionError, NoCredentialsError):
            raise

        table_desc = desc_resp.get("Table", {})
        raw_key_schema = table_desc.get("KeySchema", [])
        attr_defs: dict[str, str] = {
            a["AttributeName"]: a["AttributeType"]
            for a in table_desc.get("AttributeDefinitions", [])
        }

        # Build key_schema response dict: pk / sk with type annotation.
        key_schema: dict[str, str] = {}
        for ks in raw_key_schema:
            role = "pk" if ks["KeyType"] == "HASH" else "sk"
            attr = ks["AttributeName"]
            key_schema[role] = f"{attr} ({attr_defs.get(attr, '?')})"

        # Build GSI hints.
        gsi_hints: list[str] = []
        for gsi in table_desc.get("GlobalSecondaryIndexes", []):
            index_name = gsi.get("IndexName", "")
            gsi_key_schema = gsi.get("KeySchema", [])
            hash_attrs = [k["AttributeName"] for k in gsi_key_schema if k["KeyType"] == "HASH"]
            range_attrs = [k["AttributeName"] for k in gsi_key_schema if k["KeyType"] == "RANGE"]
            if hash_attrs:
                hint = (
                    f"{index_name} on '{hash_attrs[0]}' (HASH)"
                    " — useful for partition-key lookups"
                )
                if range_attrs:
                    hint = (
                        f"{index_name} on '{hash_attrs[0]}' (HASH), "
                        f"'{range_attrs[0]}' (RANGE) — useful for sorted range queries"
                    )
                gsi_hints.append(hint)

        # --- Scan for attribute inference ---
        from boto3.dynamodb.types import TypeDeserializer
        _deser = TypeDeserializer()

        try:
            scan_resp = client.scan(TableName=table_name, Limit=sample_size)
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        except (BotoCoreError, EndpointConnectionError, NoCredentialsError):
            raise

        raw_items = scan_resp.get("Items", [])
        sampled_count = len(raw_items)

        # Collect per-attribute stats.
        # attr_types: {attr_name: most-seen DDB type key}
        # attr_counts: {attr_name: number of items containing the attr}
        # attr_string_values: {attr_name: set of distinct string values}
        attr_counts: dict[str, int] = {}
        attr_types: dict[str, str] = {}
        attr_string_values: dict[str, set[str]] = {}

        for raw_item in raw_items:
            for attr_name, ddb_val in raw_item.items():
                # ddb_val is {"S": "..."} / {"N": "..."} / {"BOOL": True} etc.
                type_key = next(iter(ddb_val), "?")
                attr_counts[attr_name] = attr_counts.get(attr_name, 0) + 1
                # Record type (use the first seen; DynamoDB is schema-less but
                # well-behaved tables are usually consistent per attribute).
                if attr_name not in attr_types:
                    attr_types[attr_name] = type_key
                # Collect distinct string values for cardinality check.
                if type_key == "S":
                    val = ddb_val.get("S", "")
                    attr_string_values.setdefault(attr_name, set()).add(val)

        # Build attribute summary.
        attributes: dict[str, dict[str, Any]] = {}
        for attr_name, count in sorted(attr_counts.items()):
            pct = round(count / sampled_count * 100) if sampled_count > 0 else 0
            entry: dict[str, Any] = {
                "type": attr_types[attr_name],
                "present_in": f"{pct}%",
            }
            # Include sample_values for low-cardinality string attributes.
            if attr_types[attr_name] == "S":
                distinct = attr_string_values.get(attr_name, set())
                if len(distinct) <= 10:
                    entry["sample_values"] = sorted(distinct)
            attributes[attr_name] = entry

        result: dict[str, Any] = {
            "table": table_name,
            "key_schema": key_schema,
            "sampled_items": sampled_count,
            "attributes": attributes,
            "gsi_hints": gsi_hints,
        }

        if format == "table" and attributes:
            rows = [
                {"attribute": k, **v}
                for k, v in attributes.items()
            ]
            result["rendered"] = to_table(rows, title=f"Schema: {table_name}")
        else:
            result["rendered"] = None

        return result


__all__ = ["register"]
