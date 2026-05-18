"""query and scan tests, including pagination caps."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _seed_orders(call, user_id: str, n: int) -> None:
    for i in range(n):
        await call(
            "put_item",
            table_name="Orders",
            item={"user_id": user_id, "order_id": f"o_{i:03d}", "total": i * 10},
        )


async def test_query_by_partition_key(call):
    await _seed_orders(call, "u_1", 3)
    await _seed_orders(call, "u_2", 2)
    result = await call(
        "query",
        table_name="Orders",
        key_condition_expression="user_id = :u",
        expression_attribute_values={":u": "u_1"},
    )
    assert result["count"] == 3
    assert all(item["user_id"] == "u_1" for item in result["items"])


async def test_query_with_sort_key_condition(call):
    await _seed_orders(call, "u_1", 5)
    result = await call(
        "query",
        table_name="Orders",
        key_condition_expression="user_id = :u AND order_id BETWEEN :a AND :b",
        expression_attribute_values={":u": "u_1", ":a": "o_001", ":b": "o_003"},
    )
    assert result["count"] == 3


async def test_scan_returns_all_with_default_max_pages(call):
    await _seed_orders(call, "u_1", 4)
    await _seed_orders(call, "u_2", 4)
    result = await call("scan", table_name="Orders")
    assert result["count"] == 8


async def test_scan_pagination_cap(call):
    """With a low Limit + max_pages=1, scan returns only one page and surfaces LastEvaluatedKey."""
    await _seed_orders(call, "u_1", 5)
    result = await call("scan", table_name="Orders", limit=2, max_pages=1)
    assert result["pages_read"] == 1
    assert result["last_evaluated_key"] is not None


async def test_scan_resume_via_exclusive_start_key(call):
    await _seed_orders(call, "u_1", 5)
    first = await call("scan", table_name="Orders", limit=2, max_pages=1)
    assert first["last_evaluated_key"] is not None
    second = await call(
        "scan",
        table_name="Orders",
        limit=2,
        max_pages=1,
        exclusive_start_key=first["last_evaluated_key"],
    )
    # second page must contain rows we did not see in the first
    first_ids = {row["order_id"] for row in first["items"]}
    second_ids = {row["order_id"] for row in second["items"]}
    assert first_ids.isdisjoint(second_ids)


# ---------------------------------------------------------------------------
# Cost / scan protection tests
# ---------------------------------------------------------------------------


async def test_scan_without_max_read_units_returns_consumed_and_no_reason(call):
    """Without a read-unit cap, consumed_read_units is present and stopped_early_reason is None."""
    await _seed_orders(call, "u_1", 2)
    result = await call("scan", table_name="Orders")
    # consumed_read_units must always be present
    assert "consumed_read_units" in result
    assert isinstance(result["consumed_read_units"], (int, float))
    # stopped_early_reason is None when no cap is hit
    assert result["stopped_early_reason"] is None


async def test_query_without_max_read_units_returns_consumed_and_no_reason(call):
    await _seed_orders(call, "u_1", 3)
    result = await call(
        "query",
        table_name="Orders",
        key_condition_expression="user_id = :u",
        expression_attribute_values={":u": "u_1"},
    )
    assert "consumed_read_units" in result
    assert result["stopped_early_reason"] is None


async def test_scan_max_read_units_not_hit_returns_null_reason(call):
    """When budget is large enough, stopped_early_reason stays None."""
    await _seed_orders(call, "u_1", 2)
    # moto returns 1.0 per scan call; a budget of 100 won't be hit
    result = await call("scan", table_name="Orders", max_read_units=100)
    assert result["stopped_early_reason"] is None
    assert result["consumed_read_units"] >= 0.0


async def test_scan_max_read_units_cap_stops_early(call):
    """When budget of 0 is set, the very first page exceeds it and we stop early.

    moto returns 1.0 CapacityUnits per call, so a budget of 0 will be exceeded
    immediately, triggering the early-stop path.
    """
    # Seed enough items to require multiple pages at small Limit
    await _seed_orders(call, "u_1", 10)
    # Budget of 0 means even the first page (1.0 RCU in moto) exceeds it
    result = await call(
        "scan",
        table_name="Orders",
        limit=2,  # small page size to ensure multiple pages would be needed
        max_pages=10,
        max_read_units=0,
    )
    assert result["stopped_early_reason"] is not None
    assert "budget" in result["stopped_early_reason"].lower() or "exceeded" in result["stopped_early_reason"].lower()
    # consumed_read_units must be present and positive
    assert result["consumed_read_units"] > 0


async def test_query_max_read_units_cap_stops_early(call):
    """Same budget test for query."""
    await _seed_orders(call, "u_1", 10)
    result = await call(
        "query",
        table_name="Orders",
        key_condition_expression="user_id = :u",
        expression_attribute_values={":u": "u_1"},
        limit=2,
        max_pages=10,
        max_read_units=0,
    )
    assert result["stopped_early_reason"] is not None
    assert result["consumed_read_units"] > 0
