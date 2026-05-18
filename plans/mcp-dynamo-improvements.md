# Plan: `mcp-dynamo` — Post-v1 Improvements

## Overview

Improvements identified after v1 completion. Ordered roughly by implementation effort (low → high). Each item is self-contained and can be shipped independently.

---

## 1. Table Allow/Block List (whitelisting / blacklisting)

**What:** Two new optional env vars — `DYNAMODB_ALLOWED_TABLES` and `DYNAMODB_BLOCKED_TABLES` — that restrict which tables any tool can operate on. Checked at the tool-handler boundary, before any AWS call.

**Why:** Scopes a server instance to only the tables relevant to a given LLM workflow. Limits blast radius — a misconfigured or jailbroken LLM can't touch production tables it shouldn't know about.

**Design:**

- `DYNAMODB_ALLOWED_TABLES=Users,Orders` — comma-separated; if set, only these tables are accessible. All others return a structured error.
- `DYNAMODB_BLOCKED_TABLES=InternalAuditLog,BillingRecords` — comma-separated; if set, these tables are always refused, even if `ALLOWED_TABLES` is not set.
- If both are set, `ALLOWED_TABLES` takes precedence (`BLOCKED_TABLES` becomes redundant and a warning is emitted on startup).
- `list_tables` must filter its output to respect the active rule — the LLM should not even see table names it can't access.
- Enforcement lives in a new `access_control.py` module with a single `check_table_access(table_name: str)` function that raises a structured `TableAccessDenied` error.
- `config.py` parses and stores the lists as `frozenset[str] | None`.

**Error response shape:**
```json
{
  "error": "TableAccessDenied",
  "table": "BillingRecords",
  "message": "Table 'BillingRecords' is not accessible via this server instance."
}
```

**Files to change:**
1. `config.py` — add `allowed_tables`, `blocked_tables` fields
2. `src/mcp_dynamo/access_control.py` — new module, `check_table_access()`
3. `tools/items.py`, `tools/queries.py`, `tools/partiql.py`, `tools/admin.py`, `tools/discovery.py` — call `check_table_access()` at the top of each handler that accepts a `table_name`
4. `server.py` — log active rule (allowed/blocked/none) at startup, without printing table names at INFO level (keep them at DEBUG)
5. `tests/test_access_control.py` — new test file

---

## 2. Richer Error Messages Tuned for LLMs

**What:** Translate common DynamoDB boto3 exceptions into plain-English, LLM-friendly tool responses instead of surfacing raw AWS error codes.

**Why:** Raw `ConditionalCheckFailedException` or `ProvisionedThroughputExceededException` messages cause LLM retry loops and confusion. A plain-English explanation lets the LLM self-correct on the first try.

**Design:**

- New `src/mcp_dynamo/errors.py` module with an `interpret_dynamo_error(exc: ClientError) -> dict` function.
- Maps `exc.response["Error"]["Code"]` to a structured response:

| AWS Error Code | Plain-English message |
|---|---|
| `ConditionalCheckFailedException` | "The condition expression failed — the item may not exist or was modified concurrently. Check your `ConditionExpression` and retry." |
| `ProvisionedThroughputExceededException` | "Read/write capacity exhausted. Wait a moment and retry, or switch the table to on-demand billing." |
| `ResourceNotFoundException` | "Table '{table}' does not exist or is not yet active." |
| `ValidationException` | "DynamoDB rejected the request: {raw_message}. Check expression syntax and attribute names." |
| `TransactionConflictException` | "A conflicting transaction is in progress on this item. Retry after a short delay." |
| `ItemCollectionSizeLimitExceededException` | "The item collection (partition key '{pk}') has reached the 10 GB limit." |
| `RequestLimitExceeded` | "AWS request rate limit hit. Slow down and retry with exponential backoff." |

- All tool handlers wrap their AWS calls in a `try/except ClientError` that delegates to `interpret_dynamo_error()`.
- Unknown error codes fall through to a generic wrapper that still includes the code and HTTP status for debuggability.

**Files to change:**
1. `src/mcp_dynamo/errors.py` — new module
2. `tools/items.py`, `tools/queries.py`, `tools/partiql.py`, `tools/admin.py` — add `try/except ClientError` blocks
3. `tests/test_errors.py` — new test file using `moto` + manual exception injection

---

## 3. Cost / Scan Protection (`max_consumed_capacity`)

**What:** Add an optional `max_read_units` parameter to `scan` and `query`. If the cumulative `ConsumedCapacity` returned by DynamoDB exceeds the threshold, pagination stops early and a warning is included in the response.

**Why:** Page count caps (`max_pages`) bound iterations but not cost — a scan on a table with large items can consume thousands of RCUs in five pages. A capacity budget gives operators a second, cost-aware safety net.

**Design:**

- `scan` and `query` accept `max_read_units: int | None = None` (default `None` = no cap).
- Requires `ReturnConsumedCapacity="TOTAL"` on every paginated call.
- Accumulate `ConsumedCapacity.CapacityUnits` across pages; stop and return early if exceeded.
- Add `consumed_read_units: float` and `stopped_early_reason: str | None` to the response envelope.
- Document in the tool docstring that provisioned tables return exact RCU counts; on-demand tables return estimates.

**Files to change:**
1. `tools/queries.py` — update `_paginate()` helper
2. `tests/test_queries.py` — add capacity-cap test cases

---

## 4. Schema Inference Tool (`infer_schema`)

**What:** A new read-only tool that samples up to N items from a table (via `scan`) and returns the de-facto attribute types, cardinality estimates, and access pattern hints.

**Why:** LLMs orienting to an unfamiliar table currently have to manually explore via `scan` + `describe_table`. `infer_schema` collapses that into a single call and produces structured output the LLM can reason over directly.

**Design:**

- `infer_schema(table_name, sample_size=100, format="json")` — always uses `scan` internally with `Limit=sample_size`.
- Returns:
  ```json
  {
    "table": "Orders",
    "key_schema": {"pk": "order_id (S)", "sk": "created_at (N)"},
    "sampled_items": 100,
    "attributes": {
      "order_id": {"type": "S", "present_in": "100%"},
      "status":   {"type": "S", "present_in": "98%", "sample_values": ["PENDING", "SHIPPED", "DELIVERED"]},
      "amount":   {"type": "N", "present_in": "100%"},
      "tags":     {"type": "L", "present_in": "42%"}
    },
    "gsi_hint": "status-index on 'status' (GSI) — useful for filtering by order status"
  }
  ```
- `sample_values` is only included for low-cardinality string attributes (≤ 10 distinct values in the sample).
- Respects the allow/block list from improvement #1.

**Files to change:**
1. `tools/discovery.py` — add `infer_schema` tool
2. `tests/test_discovery.py` — new test cases (or new file)

---

## 5. HTTP/SSE Transport (v2)

**What:** Add an optional HTTP+SSE transport mode alongside the existing stdio transport.

**Why:** Enables multi-client setups (multiple LLM sessions sharing one server) and remote hosting (e.g., a team-shared DynamoDB proxy). Currently `MCP_TRANSPORT` accepts only `"stdio"`.

**Design:**

- `MCP_TRANSPORT=http` starts a Uvicorn/Starlette server on `MCP_PORT` (default 8080).
- Auth: API key via `Authorization: Bearer <token>` header; key set via `MCP_API_KEY` env var. Hard-fail at startup if transport is `http` and `MCP_API_KEY` is not set.
- TLS: out of scope — expected to run behind a reverse proxy (nginx, ALB).
- `pyproject.toml` adds `uvicorn` and `starlette` as optional deps under `[project.optional-dependencies] http = [...]`.
- The FastMCP `http` transport support is used directly (already in the `mcp` SDK).

**Files to change:**
1. `server.py` — extend transport gate to handle `"http"`
2. `config.py` — add `mcp_port`, `mcp_api_key` fields
3. `pyproject.toml` — add `[http]` optional dep group
4. `README.md` — add HTTP transport config snippet
5. `Dockerfile` — expose port 8080, document `MCP_TRANSPORT=http` usage

---

## Prioritization Summary

| # | Feature | Effort | Impact |
|---|---|---|---|
| 1 | Table allow/block list | Medium | High — security & scoping |
| 2 | Richer LLM error messages | Low | High — reduces LLM retry loops |
| 3 | Cost / scan protection | Low | Medium — operational safety |
| 4 | Schema inference tool | Medium | Medium — LLM DX |
| 5 | HTTP/SSE transport | High | Medium — multi-client setups |

Recommended order: **2 → 3 → 1 → 4 → 5**
