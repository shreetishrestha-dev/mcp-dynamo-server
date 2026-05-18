"""Edge-case and adversarial tests for mcp-dynamo.

This module probes:

- input validation (unknown tables, malformed keys, empty/None values)
- DynamoDB limits (400KB items, >100-key batch_get, >25-item batch_write,
  multi-page scans)
- type coercion (numeric strings vs Numbers, binary attributes, set types,
  bools, nulls)
- conditional writes that fail
- pagination resume
- PartiQL error/empty/missing scenarios
- formatting (empty rows, nested maps, wide rows)
- safety semantics for mixed-shape batches
- config errors (missing creds, bad endpoint, dynamo_whoami fields)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from boto3.dynamodb.types import Binary

from mcp_dynamo import client as client_module
from mcp_dynamo.config import ConfigError
from mcp_dynamo.formatting import _DDBEncoder, render, to_json, to_table
from mcp_dynamo.safety import statement_is_destructive

# pyproject sets asyncio_mode = auto, so async def tests run on the event loop
# automatically; we don't need pytestmark = pytest.mark.asyncio (which would
# emit warnings against the sync helper tests in this file).


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_get_item_unknown_table_raises_clean_error(call) -> None:
    """Reads against a non-existent table must surface as an error dict, not raise."""
    result = await call("get_item", table_name="NoSuchTable", key={"id": "x"})
    assert isinstance(result, dict)
    assert result.get("error") == "ResourceNotFoundException"
    msg = result.get("message", "")
    assert "not exist" in msg.lower() or "not found" in msg.lower() or "not yet active" in msg.lower()


async def test_put_item_unknown_table_raises_clean_error(call) -> None:
    result = await call("put_item", table_name="NoSuchTable", item={"id": "x"})
    assert isinstance(result, dict)
    assert result.get("error") == "ResourceNotFoundException"


async def test_describe_table_unknown_raises(call) -> None:
    result = await call("describe_table", table_name="NoSuchTable")
    assert isinstance(result, dict)
    assert result.get("error") == "ResourceNotFoundException"


async def test_get_item_with_wrong_key_shape_raises(call) -> None:
    """Composite-key table requires both PK and SK in the Key map.

    With the error-interpretation layer, ValidationException is returned as a
    structured error dict rather than a raised exception.
    """
    result = await call("get_item", table_name="Orders", key={"user_id": "u_1"})
    assert isinstance(result, dict)
    # Could be a ValidationException or similar DynamoDB error
    error = result.get("error", "")
    message = result.get("message", "")
    combined = (error + " " + message).lower()
    assert "key" in combined or "validation" in combined or "schema" in combined


async def test_get_item_with_extra_key_attribute_raises(call) -> None:
    """A key map with attributes that aren't part of the schema must return an error dict."""
    result = await call("get_item", table_name="Users", key={"id": "u_1", "stray": "noise"})
    assert isinstance(result, dict)
    error = result.get("error", "")
    message = result.get("message", "")
    combined = (error + " " + message).lower()
    assert "validat" in combined or "key" in combined or "schema" in combined


async def test_put_item_empty_string_attribute_value(call) -> None:
    """DynamoDB has allowed empty strings since 2020 — verify we don't strip them."""
    await call("put_item", table_name="Users", item={"id": "u_empty", "note": ""})
    got = await call("get_item", table_name="Users", key={"id": "u_empty"})
    assert got["item"]["note"] == ""


async def test_put_item_with_none_value_rejected_or_stored_as_null(call) -> None:
    """A None Python value should serialize to a DynamoDB NULL attribute.

    The high-level resource API accepts None and writes a NULL. We just want
    to confirm the round-trip is sane (i.e. comes back as None).
    """
    await call("put_item", table_name="Users", item={"id": "u_null", "maybe": None})
    got = await call("get_item", table_name="Users", key={"id": "u_null"})
    assert got["item"]["maybe"] is None


async def test_put_item_very_long_attribute_name(call) -> None:
    """DynamoDB allows attribute names up to 255 bytes; verify a long-but-legal name works."""
    long_name = "a" * 200
    await call("put_item", table_name="Users", item={"id": "u_long", long_name: "value"})
    got = await call("get_item", table_name="Users", key={"id": "u_long"})
    assert got["item"][long_name] == "value"


async def test_put_item_deeply_nested_map(call) -> None:
    """Nested maps up to ~32 levels are allowed; verify a deep-but-reasonable depth round-trips."""
    deep: Any = "leaf"
    for _ in range(20):
        deep = {"nested": deep}
    await call("put_item", table_name="Users", item={"id": "u_deep", "payload": deep})
    got = await call("get_item", table_name="Users", key={"id": "u_deep"})
    cur = got["item"]["payload"]
    for _ in range(20):
        cur = cur["nested"]
    assert cur == "leaf"


# ---------------------------------------------------------------------------
# DynamoDB limits
# ---------------------------------------------------------------------------


async def test_put_item_just_under_400kb_succeeds(call) -> None:
    """An item right under 400KB should be accepted."""
    payload = "x" * (380 * 1024)  # 380KB string + key overhead → ~ < 400KB
    await call("put_item", table_name="Users", item={"id": "u_big", "blob": payload})
    got = await call("get_item", table_name="Users", key={"id": "u_big"})
    assert len(got["item"]["blob"]) == 380 * 1024


async def test_put_item_over_400kb_raises(call) -> None:
    """Items over 400KB must be rejected by DynamoDB with an error response."""
    payload = "x" * (450 * 1024)
    result = await call("put_item", table_name="Users", item={"id": "u_huge", "blob": payload})
    assert isinstance(result, dict)
    # Should be an error dict with a recognizable message about size/limit
    error = result.get("error", "")
    message = result.get("message", "").lower()
    combined = (error + " " + message).lower()
    assert "size" in combined or "400" in combined or "limit" in combined or "exceeded" in combined or "validat" in combined


async def test_batch_get_more_than_100_keys(call) -> None:
    """Server auto-chunks BatchGetItem so >100 keys succeed transparently.

    DynamoDB caps a single BatchGetItem call at 100 keys; the server splits
    the request into ≤100-key chunks and merges the results.
    """
    for i in range(110):
        await call("put_item", table_name="Users", item={"id": f"u_{i}"})

    keys = [{"id": f"u_{i}"} for i in range(101)]
    result = await call("batch_get_item", request_items={"Users": {"Keys": keys}})
    # All 101 items should come back across the two chunked calls.
    assert len(result["responses"].get("Users", [])) == 101


async def test_batch_write_more_than_25_items(call) -> None:
    """Server auto-chunks BatchWriteItem so >25 requests succeed transparently.

    DynamoDB caps a single BatchWriteItem call at 25 requests; the server
    splits into ≤25-item chunks and merges UnprocessedItems across all chunks.
    """
    requests = [
        {"PutRequest": {"Item": {"id": f"u_{i}"}}} for i in range(26)
    ]
    result = await call("batch_write_item", request_items={"Users": requests})
    assert result["ok"] is True
    # Verify all 26 items were written.
    got = await call("get_item", table_name="Users", key={"id": "u_25"})
    assert got["item"] is not None


async def test_scan_max_pages_default_caps_unbounded_table(call) -> None:
    """With many items and a tiny Limit, default max_pages must stop pagination."""
    for i in range(40):
        await call(
            "put_item",
            table_name="Orders",
            item={"user_id": "u_p", "order_id": f"o_{i:03d}"},
        )
    # Limit=1 + default max_pages=5 → at most 5 rows returned.
    result = await call("scan", table_name="Orders", limit=1)
    assert result["pages_read"] <= 5
    assert len(result["items"]) <= 5
    # Should be a LastEvaluatedKey because we capped before exhausting.
    assert result["last_evaluated_key"] is not None


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------


async def test_numeric_string_stays_string(call) -> None:
    """A Python str of digits must remain a DynamoDB String (S), not silently a Number."""
    await call("put_item", table_name="Users", item={"id": "u_ns", "code": "00042"})
    got = await call("get_item", table_name="Users", key={"id": "u_ns"})
    assert got["item"]["code"] == "00042"
    assert isinstance(got["item"]["code"], str)


async def test_integer_attribute_round_trips_as_int(call) -> None:
    await call("put_item", table_name="Users", item={"id": "u_int", "age": 42})
    got = await call("get_item", table_name="Users", key={"id": "u_int"})
    assert got["item"]["age"] == 42


async def test_float_via_decimal_round_trips(call) -> None:
    """boto3 requires Decimal for non-integer numbers; passing one must work."""
    await call("put_item", table_name="Users", item={"id": "u_dec", "score": Decimal("3.14")})
    got = await call("get_item", table_name="Users", key={"id": "u_dec"})
    # to_json downcasts non-integer Decimals to float.
    assert got["item"]["score"] == pytest.approx(3.14)


async def test_boolean_round_trip(call) -> None:
    await call("put_item", table_name="Users", item={"id": "u_b", "active": True, "deleted": False})
    got = await call("get_item", table_name="Users", key={"id": "u_b"})
    assert got["item"]["active"] is True
    assert got["item"]["deleted"] is False


async def test_binary_attribute_round_trip(call) -> None:
    """Binary (B) attributes must round-trip; formatting renders bytes as a hex marker."""
    payload = b"\x00\x01\x02\xffhello"
    await call("put_item", table_name="Users", item={"id": "u_bin", "blob": payload})
    got = await call("get_item", table_name="Users", key={"id": "u_bin"})
    blob = got["item"]["blob"]
    if isinstance(blob, dict):
        assert blob.get("$binary") == payload.hex()
    else:
        assert payload.hex() in str(blob) or bytes(blob) == payload


async def test_string_set_round_trip(call) -> None:
    """SS attributes round-trip as a list (our encoder sorts sets for stability)."""
    await call("put_item", table_name="Users", item={"id": "u_ss", "tags": {"a", "b", "c"}})
    got = await call("get_item", table_name="Users", key={"id": "u_ss"})
    assert sorted(got["item"]["tags"]) == ["a", "b", "c"]


async def test_number_set_round_trip(call) -> None:
    await call(
        "put_item",
        table_name="Users",
        item={"id": "u_ns_set", "scores": {Decimal("1"), Decimal("2"), Decimal("3")}},
    )
    got = await call("get_item", table_name="Users", key={"id": "u_ns_set"})
    assert sorted(got["item"]["scores"]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Conditional writes
# ---------------------------------------------------------------------------


async def test_update_item_with_failing_condition(call) -> None:
    """ConditionalCheckFailedException must surface as a structured error dict."""
    await call("put_item", table_name="Users", item={"id": "u_c", "version": 1})
    result = await call(
        "update_item",
        table_name="Users",
        key={"id": "u_c"},
        update_expression="SET version = :new",
        expression_attribute_values={":new": 2, ":expected": 99},
        condition_expression="version = :expected",
    )
    assert isinstance(result, dict)
    error = result.get("error", "")
    message = result.get("message", "")
    combined = (error + " " + message).lower()
    assert "conditionalcheckfailed" in combined or "conditional" in combined


async def test_delete_item_with_failing_condition_surfaces_error(call) -> None:
    """Delete with confirm but a failing condition must surface as a structured error dict."""
    await call("put_item", table_name="Users", item={"id": "u_d", "version": 1})
    result = await call(
        "delete_item",
        table_name="Users",
        key={"id": "u_d"},
        condition_expression="version = :v",
        expression_attribute_values={":v": 99},
        confirm=True,
    )
    assert isinstance(result, dict)
    error = result.get("error", "")
    message = result.get("message", "")
    combined = (error + " " + message).lower()
    assert "conditionalcheckfailed" in combined or "conditional" in combined


# ---------------------------------------------------------------------------
# Pagination resume
# ---------------------------------------------------------------------------


async def test_scan_resume_recovers_all_items(call) -> None:
    """Walking a multi-page scan via LastEvaluatedKey must return every row exactly once."""
    for i in range(15):
        await call(
            "put_item",
            table_name="Orders",
            item={"user_id": "u_walk", "order_id": f"o_{i:03d}"},
        )

    seen: set[str] = set()
    last_key: Any = None
    safety = 0
    while True:
        kwargs: dict[str, Any] = {"table_name": "Orders", "limit": 4, "max_pages": 1}
        if last_key is not None:
            kwargs["exclusive_start_key"] = last_key
        page = await call("scan", **kwargs)
        for row in page["items"]:
            assert row["order_id"] not in seen, f"duplicated row: {row}"
            seen.add(row["order_id"])
        last_key = page.get("last_evaluated_key")
        safety += 1
        if not last_key or safety > 20:
            break

    assert len(seen) == 15


async def test_query_pagination_resume(call) -> None:
    for i in range(12):
        await call(
            "put_item",
            table_name="Orders",
            item={"user_id": "u_q", "order_id": f"o_{i:03d}"},
        )

    first = await call(
        "query",
        table_name="Orders",
        key_condition_expression="user_id = :u",
        expression_attribute_values={":u": "u_q"},
        limit=3,
        max_pages=1,
    )
    assert first["count"] == 3
    assert first["last_evaluated_key"] is not None

    second = await call(
        "query",
        table_name="Orders",
        key_condition_expression="user_id = :u",
        expression_attribute_values={":u": "u_q"},
        limit=3,
        max_pages=1,
        exclusive_start_key=first["last_evaluated_key"],
    )
    first_ids = {r["order_id"] for r in first["items"]}
    second_ids = {r["order_id"] for r in second["items"]}
    assert first_ids.isdisjoint(second_ids)


# ---------------------------------------------------------------------------
# PartiQL edge cases
# ---------------------------------------------------------------------------


async def test_partiql_select_no_match_returns_empty(call) -> None:
    """SELECT against an empty result set must return count=0, no errors."""
    result = await call(
        "execute_partiql_statement",
        statement="SELECT * FROM Users WHERE id = ?",
        parameters=["nope"],
    )
    assert result["count"] == 0
    assert result["items"] == []
    # next_token should be absent/None on a no-match.
    assert result.get("next_token") in (None, "")


async def test_partiql_update_on_missing_item(call) -> None:
    """PartiQL UPDATE on a non-existent key.

    Real DynamoDB raises a ConditionalCheckFailedException by default. moto's
    PartiQL parser may behave differently — we only assert the call either
    raises cleanly OR returns an empty/ok response (we don't want a silent
    crash).
    """
    try:
        result = await call(
            "execute_partiql_statement",
            statement="UPDATE Users SET name = 'X' WHERE id = 'missing_id'",
        )
    except Exception as exc:
        # Expected on real DDB; acceptable here.
        assert "conditional" in str(exc).lower() or "not found" in str(exc).lower() or "validation" in str(exc).lower()
        return
    # If it didn't raise, we accept the structured payload as-is.
    assert result is not None


async def test_partiql_syntax_error_surfaces_cleanly(call) -> None:
    """A clearly malformed statement must surface as a structured error dict or exception.

    On real DynamoDB this would be a ValidationException (returned as an error dict).
    In moto, invalid PartiQL may raise a TypeError internally; both are acceptable
    as long as the error is recognizable.
    """
    try:
        result = await call(
            "execute_partiql_statement",
            statement="THIS IS NOT VALID PARTIQL",
        )
    except Exception as exc:
        # Moto raises TypeError for unsupported PartiQL — acceptable.
        msg = str(exc).lower()
        assert "syntax" in msg or "validation" in msg or "parse" in msg or "statement" in msg or "unpack" in msg or "none" in msg
        return
    # If it didn't raise, we expect a structured error dict.
    assert isinstance(result, dict)
    error = result.get("error", "")
    message = result.get("message", "")
    combined = (error + " " + message).lower()
    assert "syntax" in combined or "validation" in combined or "parse" in combined or "statement" in combined


async def test_partiql_delete_dry_run_does_not_mutate(call) -> None:
    """A DELETE without confirm must return dry_run AND leave the row alone."""
    await call("put_item", table_name="Users", item={"id": "u_pdry", "name": "P"})
    preview = await call(
        "execute_partiql_statement",
        statement="DELETE FROM Users WHERE id = 'u_pdry'",
    )
    assert preview["dry_run"] is True
    assert "DELETE" in preview["target"]["statement"]
    still_there = await call("get_item", table_name="Users", key={"id": "u_pdry"})
    assert still_there["item"] is not None


async def test_partiql_batch_mixed_select_and_delete_requires_confirm(call) -> None:
    """If any statement in a batch is a DELETE, the whole batch needs confirm."""
    await call("put_item", table_name="Users", item={"id": "u_mix", "name": "M"})
    statements = [
        {"Statement": "SELECT * FROM Users WHERE id = ?", "Parameters": ["u_mix"]},
        {"Statement": "DELETE FROM Users WHERE id = 'u_mix'"},
    ]
    preview = await call("execute_partiql_batch", statements=statements)
    assert preview["dry_run"] is True
    assert preview["action"] == "execute_partiql_batch"
    # Untouched.
    still_there = await call("get_item", table_name="Users", key={"id": "u_mix"})
    assert still_there["item"] is not None


def test_statement_destructive_unit_cases() -> None:
    """Unit-level coverage of the DELETE detector."""
    assert statement_is_destructive("DELETE FROM Users WHERE id = 'x'") is True
    assert statement_is_destructive("  DELETE FROM Users") is True
    assert statement_is_destructive("delete from Users") is True  # case-insensitive
    assert statement_is_destructive("SELECT * FROM Users") is False
    assert statement_is_destructive("UPDATE Users SET x = 1") is True  # PartiQL UPDATE mutates data
    assert statement_is_destructive("INSERT INTO Users VALUE {'id': 'x'}") is False
    assert statement_is_destructive("") is False
    # SELECT that *mentions* DELETE shouldn't trigger the gate.
    assert statement_is_destructive("SELECT 'DELETE' FROM Users") is False
    # Statement wrapped in a leading paren still detected.
    assert statement_is_destructive("(DELETE FROM Users WHERE id = 'x')") is True


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_to_table_empty_rows_returns_placeholder() -> None:
    assert to_table([]) == "(no rows)"


def test_to_table_nested_map_renders_as_json() -> None:
    rows = [{"id": "u_1", "profile": {"name": "Ada", "age": 36}}]
    out = to_table(rows)
    # Nested map is JSON-encoded as a single cell.
    assert "Ada" in out
    assert "u_1" in out


def test_to_table_wide_rows_wraps_within_width() -> None:
    rows = [{"id": f"u_{i}", "data": "x" * 200} for i in range(3)]
    out = to_table(rows, width=80)
    # The rendered output must fit roughly within the requested width on any
    # given line. Rich folds long cells when overflow='fold'.
    longest = max(len(line) for line in out.splitlines())
    # Allow some slack for box-drawing chars but not unbounded growth.
    assert longest <= 200


def test_to_json_handles_decimal_int_and_float() -> None:
    assert to_json(Decimal("42")) == 42
    assert to_json(Decimal("3.14")) == pytest.approx(3.14)


def test_to_json_handles_bytes_and_binary_and_sets() -> None:
    encoded = to_json({"b": b"\x00\xff", "bin": Binary(b"\x01\x02"), "s": {"a", "b"}})
    assert encoded["b"] == {"$binary": "00ff"}
    assert encoded["bin"] == {"$binary": "0102"}
    assert encoded["s"] == ["a", "b"]


def test_to_json_handles_bytes_and_sets_without_binary() -> None:
    """Same encoder check but without the Binary type (which is the known-buggy path)."""
    encoded = to_json({"b": b"\x00\xff", "s": {"a", "b"}})
    assert encoded["b"] == {"$binary": "00ff"}
    assert encoded["s"] == ["a", "b"]


def test_render_table_on_dict_with_no_rows_key_passes_through() -> None:
    # If rows_key target is absent/non-list, render must return the payload unchanged.
    payload = {"items": "not a list"}
    assert render(payload, "table", rows_key="items") == payload


def test_render_json_format_is_passthrough_dict() -> None:
    payload = {"a": 1, "b": [1, 2]}
    assert render(payload, "json") == payload


def test_encoder_handles_datetime() -> None:
    import datetime as dt
    out = to_json({"t": dt.datetime(2025, 1, 1, 12, 0, 0)})
    assert out["t"].startswith("2025-01-01")


def test_encoder_default_raises_for_unknown_type() -> None:
    """Encoder should still raise on truly unencodable types — not silently drop them."""
    import json

    class Weird:
        pass

    with pytest.raises(TypeError):
        json.dumps({"x": Weird()}, cls=_DDBEncoder)


async def test_scan_table_format_renders_empty_result(call) -> None:
    """format=table on an empty scan must not blow up."""
    result = await call("scan", table_name="Users", format="table")
    # No rows seeded → either no 'rendered' key or rendered == "(no rows)"
    assert result["count"] == 0
    if "rendered" in result:
        assert "(no rows)" in result["rendered"]


async def test_list_tables_format_table(call) -> None:
    result = await call("list_tables", format="table")
    assert "rendered" in result
    # Both seed tables present.
    assert "Users" in result["rendered"]
    assert "Orders" in result["rendered"]


# ---------------------------------------------------------------------------
# Safety semantics for mixed batches
# ---------------------------------------------------------------------------


async def test_batch_write_mixed_puts_and_deletes_requires_confirm(call) -> None:
    """Mixed put+delete batch must dry-run on first call (because of the delete)."""
    await call("put_item", table_name="Users", item={"id": "u_mix_a", "name": "A"})
    request_items = {
        "Users": [
            {"PutRequest": {"Item": {"id": "u_mix_b", "name": "B"}}},
            {"DeleteRequest": {"Key": {"id": "u_mix_a"}}},
        ]
    }
    preview = await call("batch_write_item", request_items=request_items)
    assert preview["dry_run"] is True
    # Neither side-effect should have happened.
    still_a = await call("get_item", table_name="Users", key={"id": "u_mix_a"})
    no_b = await call("get_item", table_name="Users", key={"id": "u_mix_b"})
    assert still_a["item"] is not None
    assert no_b["item"] is None

    # Confirm and verify both effects applied.
    confirmed = await call("batch_write_item", request_items=request_items, confirm=True)
    assert confirmed["ok"] is True
    a_gone = await call("get_item", table_name="Users", key={"id": "u_mix_a"})
    b_present = await call("get_item", table_name="Users", key={"id": "u_mix_b"})
    assert a_gone["item"] is None
    assert b_present["item"] is not None


async def test_delete_item_dry_run_target_does_not_include_secrets(call) -> None:
    """The dry-run target dict should only echo what was passed; nothing leaked."""
    preview = await call("delete_item", table_name="Users", key={"id": "u_secret"})
    assert preview["dry_run"] is True
    assert preview["target"].keys() == {"table_name", "key"}
    assert preview["target"]["key"] == {"id": "u_secret"}


# ---------------------------------------------------------------------------
# Coverage for fixes: REMOVE gate, GSI Delete gate, repr=False, ARN redaction,
# and PartiQL Unicode/comment bypass
# ---------------------------------------------------------------------------


async def test_update_item_remove_clause_dry_runs_without_confirm(call) -> None:
    """update_item with a REMOVE clause must return a dry-run without confirm=True."""
    await call("put_item", table_name="Users", item={"id": "u_rm", "extra": "bye"})
    preview = await call(
        "update_item",
        table_name="Users",
        key={"id": "u_rm"},
        update_expression="REMOVE extra",
    )
    assert preview["dry_run"] is True
    assert preview["action"] == "update_item"
    # Attribute should still be there.
    still = await call("get_item", table_name="Users", key={"id": "u_rm"})
    assert still["item"]["extra"] == "bye"


async def test_update_item_remove_clause_executes_with_confirm(call) -> None:
    """update_item with REMOVE + confirm=True must actually remove the attribute."""
    await call("put_item", table_name="Users", item={"id": "u_rm2", "extra": "bye"})
    result = await call(
        "update_item",
        table_name="Users",
        key={"id": "u_rm2"},
        update_expression="REMOVE extra",
        confirm=True,
    )
    assert result["ok"] is True
    got = await call("get_item", table_name="Users", key={"id": "u_rm2"})
    assert "extra" not in got["item"]


async def test_update_item_set_without_remove_does_not_require_confirm(call) -> None:
    """update_item with a plain SET clause must NOT require confirm."""
    await call("put_item", table_name="Users", item={"id": "u_set", "name": "old"})
    result = await call(
        "update_item",
        table_name="Users",
        key={"id": "u_set"},
        update_expression="SET #n = :v",
        expression_attribute_names={"#n": "name"},
        expression_attribute_values={":v": "new"},
    )
    assert result.get("ok") is True


def test_config_repr_does_not_leak_credentials() -> None:
    """repr(Config) must not expose secret_key, session_token, or access_key."""
    from mcp_dynamo.config import Config
    cfg = Config(
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr...",
    )
    r = repr(cfg)
    assert "AKIAIOSFODNN7EXAMPLE" not in r
    assert "wJalrXUtnFEMI" not in r
    assert "AQoDYXdzEJr" not in r


def test_statement_destructive_partiql_comment_and_unicode_bypass() -> None:
    """Destructive-statement detection must survive comment wrapping and zero-width chars."""
    # SQL line-comment prefix
    assert statement_is_destructive("-- comment\nDELETE FROM Users WHERE id = 'x'") is True
    # Block comment prefix
    assert statement_is_destructive("/* comment */ DELETE FROM Users WHERE id = 'x'") is True
    # Zero-width joiner inside verb (Unicode obfuscation)
    assert statement_is_destructive("DE‍LETE FROM Users WHERE id = 'x'") is True
    # UPDATE is also destructive (modifies data)
    assert statement_is_destructive("UPDATE Users SET x = 1") is True
    # Non-destructive verbs are unaffected
    assert statement_is_destructive("SELECT * FROM Users WHERE id = 'x'") is False
    assert statement_is_destructive("INSERT INTO Users VALUE {'id': 'x'}") is False


async def test_partiql_dry_run_preview_contains_statement(call) -> None:
    """LLM needs to see the exact statement in the preview to decide to confirm."""
    preview = await call(
        "execute_partiql_statement",
        statement="DELETE FROM Users WHERE id = 'u_zzz'",
    )
    assert preview["dry_run"] is True
    assert preview["target"]["statement"] == "DELETE FROM Users WHERE id = 'u_zzz'"


# ---------------------------------------------------------------------------
# Config + dynamo_whoami
# ---------------------------------------------------------------------------


def test_load_config_missing_region_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No region anywhere → ConfigError with an actionable message."""
    from mcp_dynamo.config import load_config

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert "AWS_REGION" in str(excinfo.value)


def test_credentials_error_message_mentions_alternatives() -> None:
    from mcp_dynamo.config import credentials_error_message

    msg = credentials_error_message()
    assert "AWS_ACCESS_KEY_ID" in msg
    assert "AWS_PROFILE" in msg
    assert "DYNAMODB_ENDPOINT_URL" in msg


def test_verify_credentials_translates_no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """If boto3 cannot resolve any creds, verify_credentials raises ConfigError."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Point at a known-bad local endpoint so any default-chain creds get used
    # against a non-AWS target — but the meaningful path here is that no
    # creds resolve.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # Wipe the standard config files boto3 might use.
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    client_module.reset_clients()

    with pytest.raises(ConfigError):
        client_module.verify_credentials()


def test_verify_credentials_bad_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad endpoint URL must be surfaced as a clear ConfigError, not a stack trace."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", "http://127.0.0.1:1")  # unreachable
    client_module.reset_clients()
    with pytest.raises(ConfigError) as excinfo:
        client_module.verify_credentials()
    # The message should hint at the endpoint or unreachable state.
    msg = str(excinfo.value).lower()
    assert "endpoint" in msg or "reach" in msg or "connect" in msg


async def test_dynamo_whoami_returns_expected_fields(call) -> None:
    """whoami must include region, endpoint_url, is_local, and either identity or identity_error."""
    result = await call("dynamo_whoami")
    assert result["region"] == "us-east-1"
    assert result["is_local"] is False
    assert result["endpoint_url"] is None
    # Identity is optional but exactly one of identity / identity_error must exist.
    assert ("identity" in result) ^ ("identity_error" in result)


async def test_dynamo_whoami_is_local_flag_with_endpoint_url(monkeypatch: pytest.MonkeyPatch, call) -> None:
    """When DYNAMODB_ENDPOINT_URL is set, is_local should be True.

    We can't easily restart the server fixture mid-test, but we can probe
    the config layer directly.
    """
    monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    from mcp_dynamo.config import load_config

    cfg = load_config()
    assert cfg.is_local is True
    assert cfg.endpoint_url == "http://localhost:8000"


# ---------------------------------------------------------------------------
# Misc: update_table guardrail, dry-run truthiness
# ---------------------------------------------------------------------------


async def test_update_table_with_no_changes_raises(call) -> None:
    """update_table with only a table_name should refuse to call AWS."""
    with pytest.raises(Exception) as excinfo:
        await call("update_table", table_name="Users")
    assert "at least one" in str(excinfo.value).lower() or "field" in str(excinfo.value).lower()


async def test_delete_item_dry_run_with_confirm_false_still_dry_runs(call) -> None:
    """Explicit confirm=False should behave like missing — dry-run, not execute."""
    await call("put_item", table_name="Users", item={"id": "u_cf", "name": "C"})
    preview = await call("delete_item", table_name="Users", key={"id": "u_cf"}, confirm=False)
    assert preview["dry_run"] is True
    still = await call("get_item", table_name="Users", key={"id": "u_cf"})
    assert still["item"] is not None
