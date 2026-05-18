"""Query and scan with paginated reads.

Both tools cap the number of pages followed and surface ``LastEvaluatedKey``
so the LLM can resume by passing it back as ``exclusive_start_key``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from botocore.exceptions import ClientError
from mcp.server.fastmcp import FastMCP

from mcp_dynamo.access_control import check_table_access
from mcp_dynamo.client import get_config, get_dynamodb_resource
from mcp_dynamo.errors import interpret_dynamo_error
from mcp_dynamo.formatting import render
from mcp_dynamo.tools.items import _expression_kwargs

DEFAULT_MAX_PAGES = 5
MAX_PAGES_CEILING = 50


def _paginate(
    table_method: Callable[..., dict[str, Any]],
    base_kwargs: dict[str, Any],
    max_pages: int,
    max_items: int | None = None,
    max_read_units: int | None = None,
) -> dict[str, Any]:
    # Copy so we never mutate the caller's dict (kwargs are passed by reference
    # and could be reused across resume calls).
    base_kwargs = dict(base_kwargs)
    items: list[dict[str, Any]] = []
    last_key: dict[str, Any] | None = base_kwargs.pop("ExclusiveStartKey", None)
    pages = 0
    scanned = 0
    count = 0
    consumed_units: float = 0.0
    stopped_early_reason: str | None = None
    while True:
        kwargs = dict(base_kwargs)
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table_method(**kwargs)
        items.extend(resp.get("Items", []))
        count += resp.get("Count", 0)
        scanned += resp.get("ScannedCount", 0)
        consumed_units += resp.get("ConsumedCapacity", {}).get("CapacityUnits", 0.0)
        last_key = resp.get("LastEvaluatedKey")
        pages += 1
        if max_read_units is not None and consumed_units > max_read_units:
            stopped_early_reason = (
                f"Read unit budget of {max_read_units} exceeded "
                f"({consumed_units:.1f} units consumed)."
            )
            break
        if not last_key or pages >= max_pages:
            break
        if max_items is not None and len(items) >= max_items:
            break
    if max_items is not None and len(items) > max_items:
        items = items[:max_items]
    return {
        "items": items,
        "count": count,
        "scanned_count": scanned,
        "pages_read": pages,
        "last_evaluated_key": last_key,
        "consumed_read_units": consumed_units,
        "stopped_early_reason": stopped_early_reason,
    }


def _clamp_max_pages(value: int) -> int:
    """Clamp ``max_pages`` into [1, MAX_PAGES_CEILING].

    Hard ceiling defends against accidental DoS (an LLM passing ``max_pages``
    in the millions) while keeping the default behaviour of cooperative
    pagination available.
    """
    return min(max(1, int(value)), MAX_PAGES_CEILING)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def query(
        table_name: str,
        key_condition_expression: str,
        expression_attribute_values: dict[str, Any] | None = None,
        expression_attribute_names: dict[str, str] | None = None,
        filter_expression: str | None = None,
        projection_expression: str | None = None,
        index_name: str | None = None,
        scan_index_forward: bool = True,
        limit: int | None = None,
        exclusive_start_key: dict[str, Any] | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_items: int | None = None,
        max_read_units: int | None = None,
        consistent_read: bool = False,
        format: Literal["json", "table"] = "json",
    ) -> dict[str, Any]:
        """Query a table or index using a ``KeyConditionExpression``.

        ``KeyConditionExpression`` must reference the partition key (and
        optionally the sort key) of the table or the chosen ``index_name``.
        ``filter_expression`` is applied after the key match — it does not
        reduce read cost.

        Pagination follows up to ``max_pages`` pages (default 5, hard cap 50).
        If the result is truncated, ``last_evaluated_key`` is returned; pass it
        back as ``exclusive_start_key`` to resume. ``max_items`` is an
        additional cap on accumulated item count.

        ``max_read_units`` sets a read-capacity budget across all pages. If
        cumulative ``ConsumedCapacity.CapacityUnits`` exceeds this value,
        pagination stops early and ``stopped_early_reason`` is set in the
        response. Provisioned tables return exact RCU counts; on-demand tables
        return estimates.

        ``ConsistentRead`` is only sent to DynamoDB when ``consistent_read=True``
        — DynamoDB rejects ``ConsistentRead=True`` on GSIs, so always-sending
        it would break GSI queries.

        Example
        -------
        ``query(
            table_name="Orders",
            key_condition_expression="user_id = :u",
            expression_attribute_values={":u": "u_123"}
        )``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        resource = get_dynamodb_resource()
        table = resource.Table(table_name)
        max_pages = _clamp_max_pages(max_pages)
        kwargs: dict[str, Any] = {"ScanIndexForward": scan_index_forward}
        if consistent_read:
            kwargs["ConsistentRead"] = True
        if max_read_units is not None:
            kwargs["ReturnConsumedCapacity"] = "TOTAL"
        kwargs.update(
            _expression_kwargs(
                names=expression_attribute_names,
                values=expression_attribute_values,
                projection=projection_expression,
                filter=filter_expression,
                key_condition=key_condition_expression,
            )
        )
        if index_name:
            kwargs["IndexName"] = index_name
        if limit is not None:
            kwargs["Limit"] = limit
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        try:
            result = _paginate(
                table.query, kwargs, max_pages=max_pages, max_items=max_items,
                max_read_units=max_read_units,
            )
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        rendered = render(result, format, rows_key="items", title=f"Query: {table_name}")
        if format == "table":
            return rendered  # type: ignore[no-any-return]
        out = dict(rendered) if isinstance(rendered, dict) else result
        out.setdefault("rendered", None)
        return out

    @mcp.tool()
    def scan(
        table_name: str,
        filter_expression: str | None = None,
        expression_attribute_values: dict[str, Any] | None = None,
        expression_attribute_names: dict[str, str] | None = None,
        projection_expression: str | None = None,
        index_name: str | None = None,
        limit: int | None = None,
        exclusive_start_key: dict[str, Any] | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_items: int | None = None,
        max_read_units: int | None = None,
        consistent_read: bool = False,
        format: Literal["json", "table"] = "json",
    ) -> dict[str, Any]:
        """Scan an entire table or index, with hard pagination caps.

        Scans are expensive — prefer ``query`` when you have the partition key.
        This tool follows up to ``max_pages`` pages (default 5, hard cap 50)
        so an LLM cannot accidentally read a billion-row table into context.
        ``max_items`` adds an item-count cap on top. The returned
        ``last_evaluated_key`` lets the caller resume.

        ``max_read_units`` sets a read-capacity budget across all pages. If
        cumulative ``ConsumedCapacity.CapacityUnits`` exceeds this value,
        pagination stops early and ``stopped_early_reason`` is set in the
        response. Provisioned tables return exact RCU counts; on-demand tables
        return estimates.

        ``ConsistentRead`` is only sent to DynamoDB when ``consistent_read=True``
        — DynamoDB rejects ``ConsistentRead=True`` on GSIs.

        Example
        -------
        ``scan(table_name="Users", filter_expression="begins_with(name, :p)",
              expression_attribute_values={":p": "A"})``
        """
        if denied := check_table_access(table_name, get_config()):
            return denied
        resource = get_dynamodb_resource()
        table = resource.Table(table_name)
        max_pages = _clamp_max_pages(max_pages)
        kwargs: dict[str, Any] = {}
        if consistent_read:
            kwargs["ConsistentRead"] = True
        if max_read_units is not None:
            kwargs["ReturnConsumedCapacity"] = "TOTAL"
        kwargs.update(
            _expression_kwargs(
                names=expression_attribute_names,
                values=expression_attribute_values,
                projection=projection_expression,
                filter=filter_expression,
            )
        )
        if index_name:
            kwargs["IndexName"] = index_name
        if limit is not None:
            kwargs["Limit"] = limit
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        try:
            result = _paginate(
                table.scan, kwargs, max_pages=max_pages, max_items=max_items,
                max_read_units=max_read_units,
            )
        except ClientError as exc:
            return interpret_dynamo_error(exc)
        rendered = render(result, format, rows_key="items", title=f"Scan: {table_name}")
        if format == "table":
            return rendered  # type: ignore[no-any-return]
        out = dict(rendered) if isinstance(rendered, dict) else result
        out.setdefault("rendered", None)
        return out


__all__ = ["register"]
