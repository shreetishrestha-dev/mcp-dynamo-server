# mcp-dynamo

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that exposes Amazon DynamoDB operations to LLM clients — Claude Desktop, Claude Code, Cursor, Windsurf, and any MCP-compatible host — over stdio.

Works with both AWS-hosted DynamoDB and DynamoDB Local.

---

## Table of contents

- [Quick start](#quick-start)
- [How this differs from other DynamoDB MCP servers](#how-this-differs-from-other-dynamodb-mcp-servers)
- [Installation](#installation)
- [Environment variables](#environment-variables)
- [MCP client setup](#mcp-client-setup)
- [DynamoDB Local](#dynamodb-local)
- [Access control](#access-control)
- [Safety model](#safety-model)
- [Tool reference](#tool-reference)
- [Output formats](#output-formats)
- [Pagination](#pagination)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Architecture](#architecture)

---

## Quick start

The fastest path: DynamoDB Local + uvx (no permanent install, no real AWS account needed).

```bash
# 1. Start DynamoDB Local
docker run -d -p 8000:8000 --name ddb-local amazon/dynamodb-local

# 2. Install uv (if you haven't)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Test the server with the MCP Inspector
AWS_ACCESS_KEY_ID=local \
AWS_SECRET_ACCESS_KEY=local \
AWS_REGION=us-east-1 \
DYNAMODB_ENDPOINT_URL=http://localhost:8000 \
npx @modelcontextprotocol/inspector uvx mcp-dynamo
```

The Inspector opens a browser UI at `localhost:5173` where you can call every tool interactively.

---

## How this differs from other DynamoDB MCP servers

Several DynamoDB MCP servers exist. Here is what sets this one apart.

### Feature comparison

| Feature | mcp-dynamo | AWS Labs DynamoDB MCP | imankamyabi | jjikky / hyunjong-dev-lab |
|---|---|---|---|---|
| Full CRUD (read + write + delete) | ✅ | ❌ modeling only | ✅ | ❌ read-only |
| PartiQL (SELECT / INSERT / UPDATE / DELETE) | ✅ | ❌ | ❌ | ❌ |
| Batch get + batch write | ✅ | ❌ | ❌ | ❌ |
| Confirm guard for destructive ops | ✅ dry-run on every delete | ❌ | ⚠️ deletes disabled entirely | ✅ deletes disabled entirely |
| Table-level allow / block lists | ✅ | ❌ | ❌ | ❌ |
| Paginated query + scan with resume tokens | ✅ max\_pages, max\_items, max\_read\_units | ❌ | ⚠️ partial | ✅ paginate-query-table only |
| Read-unit budget guard (stop early on cost) | ✅ | ❌ | ❌ | ❌ |
| DynamoDB Local support | ✅ documented + tested | ✅ | ❌ undocumented | ❌ undocumented |
| Schema inference (infer\_schema) | ✅ | ❌ | ❌ | ❌ |
| `format="table"` Rich rendering | ✅ | ❌ | ❌ | ❌ |
| Automatic chunking for batch ops | ✅ (100 keys / 25 writes) | ❌ | ❌ | ❌ |
| Python-native types (no AttributeValue maps) | ✅ | ❌ | ❌ | ❌ |
| moto-backed test suite | ✅ | ❌ | ❌ | ❌ |
| Language / runtime | Python | Python | Node.js | Node.js |

### What each alternative actually does

**[AWS Labs DynamoDB MCP Server](https://github.com/awslabs/mcp/tree/main/src/dynamodb-mcp-server)** — A data modeling and schema conversion tool, not a runtime query server. It helps you design tables and generate Pydantic models. It cannot read or write items.

**[imankamyabi/dynamodb-mcp-server](https://github.com/imankamyabi/dynamodb-mcp-server)** — Covers basic CRUD and table management but deletes are disabled wholesale (no confirm-and-proceed pattern). No PartiQL, no batch ops, no access control, no DynamoDB Local docs.

**[jjikky/dynamo-readonly-mcp](https://github.com/jjikky/dynamo-readonly-mcp) and [hyunjong-dev-lab/dynamo-mcp](https://github.com/hyunjong-dev-lab/dynamo-mcp)** — Read-only servers. Useful for safe exploration but cannot write anything. No PartiQL, no access control lists.

**[CData DynamoDB MCP](https://github.com/CDataSoftware/amazon-dynamodb-mcp-server-by-cdata)** — Read-only, requires a paid CData Connect AI subscription for write access, goes through a JDBC abstraction layer.

### Why the differences matter

**Confirm guard vs. disabled deletes.** Disabling deletes entirely means an LLM can never clean up test data, execute a migration, or handle a delete-by-design workflow. mcp-dynamo's dry-run-first pattern keeps the capability available while preventing accidental execution.

**Table-level access control.** In a real environment you may want Claude to query production read replicas but not touch write tables, or to access only a specific tenant's namespace. No other server supports this without modifying IAM policies.

**PartiQL.** Many existing DynamoDB workloads use PartiQL queries. Without it, an LLM assistant cannot help with a large class of existing code and data workflows.

**Pagination cost guard.** An LLM asked to "scan all users" on a 50 million row table can run up a significant AWS bill before the operator notices. `max_pages`, `max_items`, and `max_read_units` give hard ceilings on how much data a single tool call reads.

**Python-native types.** Other servers require the caller to pass DynamoDB wire-format dicts (`{"S": "value"}`, `{"N": "42"}`). mcp-dynamo accepts `{"id": "value", "count": 42}` and handles serialization internally, which is what an LLM naturally produces.

---

## Installation

### Option A — uvx (recommended, zero install)

[`uvx`](https://docs.astral.sh/uv/) runs the package in an isolated environment without a permanent install. Install `uv` once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Use `uvx mcp-dynamo` as the command in your MCP client config. No further steps needed.

### Option B — pip

```bash
pip install mcp-dynamo
mcp-dynamo --help   # verify
```

### Option C — pipx

```bash
pipx install mcp-dynamo
```

### Option D — Docker

```bash
docker pull shreetishrestha977/mcp-dynamo:latest
```

Build from source:

```bash
git clone https://github.com/shreetishrestha/mcp-dynamo.git
cd mcp-dynamo
docker build -t mcp-dynamo .
```

### Option E — editable / development install

```bash
git clone https://github.com/shreetishrestha/mcp-dynamo.git
cd mcp-dynamo
python3.13 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

---

## Environment variables

All configuration is passed via environment variables, either in your shell or in the `env` block of your MCP client config.

### Credentials

| Variable | Required | Description |
|---|---|---|
| `AWS_REGION` | **Yes** | AWS region, e.g. `us-east-1`. Also accepts `AWS_DEFAULT_REGION`. |
| `AWS_ACCESS_KEY_ID` | One of† | Static AWS access key. |
| `AWS_SECRET_ACCESS_KEY` | One of† | Pairs with `AWS_ACCESS_KEY_ID`. |
| `AWS_SESSION_TOKEN` | No | Temporary credentials from STS / SSO. |
| `AWS_PROFILE` | One of† | Named profile from `~/.aws/credentials` or `~/.aws/config`. |
| `DYNAMODB_ENDPOINT_URL` | No | Override the DynamoDB endpoint. Set to `http://localhost:8000` for DynamoDB Local. |

† At least one credential source is required. **Resolution order:** explicit key pair → `AWS_PROFILE` → boto3 default chain (IAM role, ECS task role, SSO session, container creds, etc.).

The server issues a lightweight `ListTables` call at startup to validate credentials. It exits immediately with an actionable message if no credentials resolve.

### Access control

| Variable | Description |
|---|---|
| `DYNAMODB_ALLOWED_TABLES` | Comma-separated list of table names the server may access. All others are denied. |
| `DYNAMODB_BLOCKED_TABLES` | Comma-separated list of table names to deny. All others are allowed. |

If both are set, `DYNAMODB_ALLOWED_TABLES` takes precedence (a warning is printed to stderr).

```bash
# Only expose two tables
DYNAMODB_ALLOWED_TABLES=prod_users,prod_orders

# Block two tables, allow everything else
DYNAMODB_BLOCKED_TABLES=staging_temp,dev_scratch
```

### Transport

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | Transport mode. Only `stdio` is supported in v1. Any other value causes a hard exit. |

---

## MCP client setup

### Claude Desktop

Config file location:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Static credentials:**

```json
{
  "mcpServers": {
    "dynamodb": {
      "command": "uvx",
      "args": ["mcp-dynamo"],
      "env": {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

**AWS profile (SSO, named profile):**

```json
{
  "mcpServers": {
    "dynamodb": {
      "command": "uvx",
      "args": ["mcp-dynamo"],
      "env": {
        "AWS_PROFILE": "my-profile",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

Restart Claude Desktop after editing the config.

---

### Claude Code (CLI)

```bash
claude mcp add dynamodb uvx mcp-dynamo \
  -e AWS_ACCESS_KEY_ID=AKIA... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -e AWS_REGION=us-east-1
```

Or edit `~/.claude.json` directly:

```json
{
  "mcpServers": {
    "dynamodb": {
      "command": "uvx",
      "args": ["mcp-dynamo"],
      "env": {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

---

### Cursor

Open **Settings → MCP** (or edit `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "dynamodb": {
      "command": "uvx",
      "args": ["mcp-dynamo"],
      "env": {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

Restart Cursor after saving.

---

### Windsurf

Open **Settings → Cascade → MCP Servers** and add the same block.

---

### VS Code (with MCP support)

Add to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "dynamodb": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-dynamo"],
      "env": {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

---

### Docker (any client)

Replace the `uvx` invocation with `docker run`:

```json
{
  "mcpServers": {
    "dynamodb": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "AWS_ACCESS_KEY_ID",
        "-e", "AWS_SECRET_ACCESS_KEY",
        "-e", "AWS_REGION",
        "shreetishrestha977/mcp-dynamo:latest"
      ],
      "env": {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

---

## DynamoDB Local

```bash
# Start DynamoDB Local
docker run -d -p 8000:8000 --name ddb-local amazon/dynamodb-local

# Run the server against it (any non-empty credential values work)
export AWS_ACCESS_KEY_ID=local
export AWS_SECRET_ACCESS_KEY=local
export AWS_REGION=us-east-1
export DYNAMODB_ENDPOINT_URL=http://localhost:8000

python -m mcp_dynamo
```

MCP client config:

```json
{
  "env": {
    "AWS_ACCESS_KEY_ID": "local",
    "AWS_SECRET_ACCESS_KEY": "local",
    "AWS_REGION": "us-east-1",
    "DYNAMODB_ENDPOINT_URL": "http://localhost:8000"
  }
}
```

`dynamo_whoami` returns `"is_local": true` when the endpoint override points at `localhost` or `127.0.0.1`.

---

## Access control

Two environment variables restrict which tables the server can touch. They are enforced at the tool boundary, before any AWS call.

### Allow list

Only the listed tables are accessible; all others return `TableAccessDenied`.

```bash
DYNAMODB_ALLOWED_TABLES=prod_users,prod_orders,prod_sessions
```

### Block list

All tables are accessible except the listed ones.

```bash
DYNAMODB_BLOCKED_TABLES=staging_scratch,temp_migration
```

### Access denied response shape

```json
{
  "error": "TableAccessDenied",
  "table": "restricted_table",
  "message": "Table 'restricted_table' is not accessible via this server instance."
}
```

`list_tables` automatically filters out denied tables so the LLM never sees table names it cannot use.

---

## Safety model

Destructive operations never execute on the first call. If `confirm` is absent or `false`, the tool returns a dry-run preview:

```json
{
  "dry_run": true,
  "action": "delete_item",
  "target": {
    "table_name": "Users",
    "key": {"id": "u_123"}
  },
  "message": "Re-call with confirm=true to execute."
}
```

To execute, repeat the call with `confirm=true`.

**Tools that require `confirm=true`:**

| Tool | Condition |
|---|---|
| `delete_item` | Always |
| `delete_table` | Always |
| `update_item` | Only when `update_expression` contains a `REMOVE` clause |
| `update_table` | Only when `global_secondary_index_updates` contains a `Delete` entry |
| `batch_write_item` | Only when any `DeleteRequest` is present |
| `execute_partiql_statement` | Only when statement verb is `DELETE` |
| `execute_partiql_batch` | Only when any statement verb is `DELETE` |

Pure-write variants (`batch_write_item` with only `PutRequest`, PartiQL `INSERT`/`UPDATE`) run without confirmation.

---

## Tool reference

All keys and item values use **Python-native types** (strings, ints, floats, booleans, lists, dicts). The server serializes to DynamoDB wire format internally.

All read tools accept a `format` parameter (`"json"` or `"table"`). See [Output formats](#output-formats).

---

### Discovery tools

#### `dynamo_whoami`

Returns the active AWS identity, region, and endpoint. Call this first to confirm you're connected to the right account.

**Parameters:** none

**Returns:**

```json
{
  "region": "us-east-1",
  "endpoint_url": null,
  "is_local": false,
  "has_endpoint_override": false,
  "identity": {
    "UserId": "AIDA...",
    "Arn": "arn:aws:iam::****6789:user/alice"
  }
}
```

The 12-digit AWS account ID in the ARN is redacted to `****NNNN` to avoid leaking it into logs or LLM transcripts.

If STS is unreachable (e.g. DynamoDB Local with placeholder creds), `identity` is replaced with `identity_error`.

---

#### `list_tables`

Lists DynamoDB table names in the active region.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `prefix` | `string \| null` | `null` | Client-side prefix filter. Case-sensitive. |
| `limit` | `int` | `100` | Maximum table names to return after filtering. |
| `format` | `"json" \| "table"` | `"json"` | Output format. |

**Returns:**

```json
{
  "tables": ["Orders", "Users"],
  "count": 2,
  "rendered": null
}
```

Tables denied by the access control allow/block list are omitted automatically.

---

#### `describe_table`

Returns the full `DescribeTable` payload for a single table: key schema, indexes, billing mode, item count, creation time.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Table to describe. |
| `format` | `"json" \| "table"` | `"json"` | Output format. |

**Example:** check schema before writing a query

```
describe_table(table_name="Orders")
```

The response includes `KeySchema`, `AttributeDefinitions`, `GlobalSecondaryIndexes`, `LocalSecondaryIndexes`, `BillingModeSummary`, `ItemCount`, and `TableStatus`.

---

#### `infer_schema`

Samples items from a table and infers the de-facto attribute schema. Useful for exploring unfamiliar tables.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Table to sample. |
| `sample_size` | `int` | `100` | Number of items to scan (single page). |
| `format` | `"json" \| "table"` | `"json"` | Output format. |

**Returns:**

```json
{
  "table": "Orders",
  "key_schema": {
    "pk": "user_id (S)",
    "sk": "order_id (S)"
  },
  "sampled_items": 100,
  "attributes": {
    "user_id": {"type": "S", "present_in": "100%"},
    "status":  {"type": "S", "present_in": "100%", "sample_values": ["pending", "shipped", "cancelled"]},
    "total":   {"type": "N", "present_in": "87%"}
  },
  "gsi_hints": [
    "status-index on 'status' (HASH), 'created_at' (RANGE) — useful for sorted range queries"
  ],
  "rendered": null
}
```

`sample_values` is only included for string (`S`) attributes with 10 or fewer distinct values in the sample.

---

### Item tools

#### `get_item`

Fetches a single item by primary key.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Target table. |
| `key` | `dict` | — | Primary key. Must include partition key and sort key (if composite). |
| `format` | `"json" \| "table"` | `"json"` | Output format. |

**Returns:**

```json
{"item": {"id": "u_123", "name": "Ada"}, "rendered": null}
```

`item` is `null` when the key does not exist.

**Example:**

```
get_item(table_name="Users", key={"id": "u_123"})
```

---

#### `put_item`

Inserts or replaces an item. An existing item at the same key is overwritten unless a `condition_expression` prevents it.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Target table. |
| `item` | `dict` | — | Full item including key attributes. |
| `condition_expression` | `string \| null` | `null` | Condition that must pass for the put to succeed. |
| `expression_attribute_names` | `dict \| null` | `null` | Placeholder substitutions for reserved attribute names (`#name → "name"`). |
| `expression_attribute_values` | `dict \| null` | `null` | Placeholder substitutions for values (`:val → "Ada"`). |
| `return_values` | `"NONE" \| "ALL_OLD"` | `"NONE"` | Return the item that was replaced. |

**Returns:**

```json
{"ok": true, "table": "Users"}
```

With `return_values="ALL_OLD"` and a replaced item: `{"ok": true, "table": "Users", "attributes": {...}}`.

**Example — insert only if key doesn't exist:**

```
put_item(
  table_name="Users",
  item={"id": "u_123", "name": "Ada"},
  condition_expression="attribute_not_exists(id)"
)
```

---

#### `update_item`

Patches an existing item using a `UpdateExpression`. Does not overwrite the whole item.

Requires `confirm=true` only when `update_expression` contains a `REMOVE` clause (attribute deletion).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Target table. |
| `key` | `dict` | — | Primary key of the item to update. |
| `update_expression` | `string` | — | DynamoDB UpdateExpression, e.g. `"SET #n = :name, version = version + :one"`. |
| `expression_attribute_names` | `dict \| null` | `null` | Name placeholder substitutions. |
| `expression_attribute_values` | `dict \| null` | `null` | Value placeholder substitutions. |
| `condition_expression` | `string \| null` | `null` | Optional condition; fails if not met. |
| `return_values` | `"NONE" \| "ALL_OLD" \| "UPDATED_OLD" \| "ALL_NEW" \| "UPDATED_NEW"` | `"ALL_NEW"` | Which attributes to return. |
| `confirm` | `bool` | `false` | Required for `REMOVE` clauses. |

**Returns:**

```json
{"ok": true, "attributes": {"id": "u_123", "name": "Ada Lovelace"}}
```

**Example:**

```
update_item(
  table_name="Users",
  key={"id": "u_123"},
  update_expression="SET #n = :name",
  expression_attribute_names={"#n": "name"},
  expression_attribute_values={":name": "Ada Lovelace"}
)
```

---

#### `delete_item`

Deletes an item by primary key. Requires `confirm=true`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Target table. |
| `key` | `dict` | — | Primary key of the item to delete. |
| `condition_expression` | `string \| null` | `null` | Delete only if condition passes. |
| `expression_attribute_names` | `dict \| null` | `null` | Name placeholder substitutions. |
| `expression_attribute_values` | `dict \| null` | `null` | Value placeholder substitutions. |
| `return_values` | `"NONE" \| "ALL_OLD"` | `"ALL_OLD"` | Return the deleted item. |
| `confirm` | `bool` | `false` | Must be `true` to execute. |

**Returns:**

```json
{"ok": true, "deleted": {"id": "u_123", "name": "Ada"}}
```

**Example:**

```
# Preview
delete_item(table_name="Users", key={"id": "u_123"})

# Execute
delete_item(table_name="Users", key={"id": "u_123"}, confirm=true)
```

---

#### `batch_get_item`

Fetches multiple items across one or more tables in a single call. Auto-chunks requests with more than 100 keys per table.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `request_items` | `dict` | — | Map of `table_name → {Keys: [...], ConsistentRead?, ProjectionExpression?, ExpressionAttributeNames?}`. |
| `format` | `"json" \| "table"` | `"json"` | Output format. |

**Shape:**

```json
{
  "Users":  {"Keys": [{"id": "u_1"}, {"id": "u_2"}]},
  "Orders": {"Keys": [{"user_id": "u_1", "order_id": "o_9"}]}
}
```

**Returns:**

```json
{
  "responses": {
    "Users":  [{"id": "u_1", "name": "Ada"}, {"id": "u_2", "name": "Grace"}],
    "Orders": [{"user_id": "u_1", "order_id": "o_9", "total": 42}]
  },
  "unprocessed_keys": {},
  "rendered": null
}
```

`unprocessed_keys` is non-empty when DynamoDB throttled some keys; retry those keys.

---

#### `batch_write_item`

Bulk puts and/or deletes across one or more tables. Auto-chunks requests with more than 25 items per table. Requires `confirm=true` when any `DeleteRequest` is present.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `request_items` | `dict` | — | Map of `table_name → [PutRequest | DeleteRequest, ...]`. |
| `confirm` | `bool` | `false` | Required when any `DeleteRequest` is present. |

**Shape:**

```json
{
  "Users": [
    {"PutRequest":    {"Item": {"id": "u_1", "name": "Ada"}}},
    {"DeleteRequest": {"Key":  {"id": "u_old"}}}
  ]
}
```

**Returns:**

```json
{"ok": true, "unprocessed_items": {}}
```

`unprocessed_items` is non-empty on partial throttle failures; retry those items.

---

### Query tools

#### `query`

Queries a table or index using a `KeyConditionExpression`. More efficient than `scan` because it only reads matching partitions.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Target table. |
| `key_condition_expression` | `string` | — | Key condition, e.g. `"user_id = :u"`. Must reference the partition key. |
| `expression_attribute_values` | `dict \| null` | `null` | Value substitutions. |
| `expression_attribute_names` | `dict \| null` | `null` | Name substitutions for reserved words. |
| `filter_expression` | `string \| null` | `null` | Post-read filter. Does not reduce read cost. |
| `projection_expression` | `string \| null` | `null` | Comma-separated attributes to return. |
| `index_name` | `string \| null` | `null` | GSI or LSI name to query instead of the base table. |
| `scan_index_forward` | `bool` | `true` | Sort order for sort-key results. `false` = descending. |
| `limit` | `int \| null` | `null` | DynamoDB page size (evaluated before `filter_expression`). |
| `exclusive_start_key` | `dict \| null` | `null` | Resume token from a previous call's `last_evaluated_key`. |
| `max_pages` | `int` | `5` | Maximum pages to follow. Hard cap: 50. |
| `max_items` | `int \| null` | `null` | Additional cap on total accumulated items. |
| `max_read_units` | `int \| null` | `null` | Stop early if cumulative read capacity units exceed this. |
| `consistent_read` | `bool` | `false` | Strongly consistent read. Not supported on GSIs. |
| `format` | `"json" \| "table"` | `"json"` | Output format. |

**Returns:**

```json
{
  "items": [...],
  "count": 10,
  "scanned_count": 10,
  "pages_read": 1,
  "last_evaluated_key": null,
  "consumed_read_units": 0.5,
  "stopped_early_reason": null,
  "rendered": null
}
```

**Example:**

```
query(
  table_name="Orders",
  key_condition_expression="user_id = :u AND begins_with(order_id, :prefix)",
  expression_attribute_values={":u": "u_123", ":prefix": "2024"}
)
```

---

#### `scan`

Reads an entire table or index. Expensive — prefer `query` when you have the partition key.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Target table. |
| `filter_expression` | `string \| null` | `null` | Condition applied after reading each page. |
| `expression_attribute_values` | `dict \| null` | `null` | Value substitutions. |
| `expression_attribute_names` | `dict \| null` | `null` | Name substitutions. |
| `projection_expression` | `string \| null` | `null` | Attributes to return. |
| `index_name` | `string \| null` | `null` | GSI or LSI to scan. |
| `limit` | `int \| null` | `null` | DynamoDB page size. |
| `exclusive_start_key` | `dict \| null` | `null` | Resume token. |
| `max_pages` | `int` | `5` | Maximum pages to follow. Hard cap: 50. |
| `max_items` | `int \| null` | `null` | Cap on total accumulated items. |
| `max_read_units` | `int \| null` | `null` | Budget for read capacity units. |
| `consistent_read` | `bool` | `false` | Strongly consistent read. Not supported on GSIs. |
| `format` | `"json" \| "table"` | `"json"` | Output format. |

Returns the same shape as `query`.

**Example:**

```
scan(
  table_name="Users",
  filter_expression="begins_with(#n, :prefix)",
  expression_attribute_names={"#n": "name"},
  expression_attribute_values={":prefix": "A"}
)
```

---

### PartiQL tools

DynamoDB PartiQL is SQL-flavored but constrained: no JOINs, no subqueries, partition key required in `WHERE` for non-scan SELECTs, string literals use single quotes, and positional placeholders are `?`.

#### `execute_partiql_statement`

Runs a single PartiQL statement. DELETE statements require `confirm=true`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `statement` | `string` | — | The PartiQL statement. |
| `parameters` | `list \| null` | `null` | Positional `?` substitutions in Python-native types. |
| `consistent_read` | `bool` | `false` | Strongly consistent read for SELECT. |
| `next_token` | `string \| null` | `null` | Resume token for paginated SELECT. |
| `limit` | `int \| null` | `null` | Max items per page. |
| `format` | `"json" \| "table"` | `"json"` | Output format. |
| `confirm` | `bool` | `false` | Required for DELETE statements. |

**Returns:**

```json
{
  "items": [...],
  "count": 5,
  "next_token": null
}
```

**Examples:**

```
execute_partiql_statement(
  statement="SELECT * FROM Users WHERE id = ?",
  parameters=["u_123"]
)

execute_partiql_statement(
  statement="INSERT INTO Users VALUE {'id': ?, 'name': ?}",
  parameters=["u_1", "Ada"]
)

execute_partiql_statement(
  statement="DELETE FROM Users WHERE id = ?",
  parameters=["u_old"],
  confirm=true
)
```

---

#### `execute_partiql_batch`

Runs up to 25 PartiQL statements in one call. Requires `confirm=true` if any statement is a DELETE.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `statements` | `list[dict]` | — | List of `{"Statement": str, "Parameters": [...], "ConsistentRead"?: bool}`. Parameters are optional. |
| `confirm` | `bool` | `false` | Required if any statement is a DELETE. |

**Returns:**

```json
{
  "ok": true,
  "responses": [
    {"item": {"id": "u_1", "name": "Ada"}},
    {"error": {"Code": "...", "Message": "..."}}
  ]
}
```

Each response corresponds to one statement in order. Failed statements carry an `error` entry; others carry `item` (for SELECT/UPDATE) or nothing (for INSERT/DELETE).

**Example:**

```
execute_partiql_batch(statements=[
  {"Statement": "INSERT INTO Users VALUE {'id': ?, 'name': ?}", "Parameters": ["u_1", "Ada"]},
  {"Statement": "INSERT INTO Users VALUE {'id': ?, 'name': ?}", "Parameters": ["u_2", "Grace"]}
])
```

---

### Admin tools

#### `create_table`

Creates a new DynamoDB table and (by default) waits until it reaches `ACTIVE` status.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Table name. |
| `key_schema` | `list[dict]` | — | `[{"AttributeName": "id", "KeyType": "HASH"}]`. Add a `RANGE` entry for composite keys. |
| `attribute_definitions` | `list[dict]` | — | `[{"AttributeName": "id", "AttributeType": "S"}]`. Include all key and index attributes. `S` = String, `N` = Number, `B` = Binary. |
| `billing_mode` | `string` | `"PAY_PER_REQUEST"` | `"PAY_PER_REQUEST"` or `"PROVISIONED"`. |
| `provisioned_throughput` | `dict \| null` | `null` | Required when `billing_mode="PROVISIONED"`. `{"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}`. |
| `global_secondary_indexes` | `list \| null` | `null` | Standard DynamoDB GSI spec. |
| `local_secondary_indexes` | `list \| null` | `null` | Standard DynamoDB LSI spec. |
| `stream_specification` | `dict \| null` | `null` | DynamoDB Streams config. |
| `tags` | `list \| null` | `null` | `[{"Key": "env", "Value": "prod"}]`. |
| `wait_until_active` | `bool` | `true` | Block until the table is `ACTIVE`. |

**Returns:** `{"ok": true, "table": {...full TableDescription...}}`

**Example:**

```
create_table(
  table_name="Tasks",
  key_schema=[
    {"AttributeName": "project_id", "KeyType": "HASH"},
    {"AttributeName": "task_id",    "KeyType": "RANGE"}
  ],
  attribute_definitions=[
    {"AttributeName": "project_id", "AttributeType": "S"},
    {"AttributeName": "task_id",    "AttributeType": "S"}
  ]
)
```

---

#### `update_table`

Modifies an existing table. Pass only the fields you want to change. Requires `confirm=true` when deleting a GSI (irreversible).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Target table. |
| `attribute_definitions` | `list \| null` | `null` | New attribute definitions (required when adding a new index). |
| `billing_mode` | `string \| null` | `null` | Switch billing mode. |
| `provisioned_throughput` | `dict \| null` | `null` | New throughput when in `PROVISIONED` mode. |
| `global_secondary_index_updates` | `list \| null` | `null` | `[{"Create": {...}}, {"Update": {...}}, {"Delete": {"IndexName": "..."}}]`. |
| `stream_specification` | `dict \| null` | `null` | Enable/disable Streams. |
| `confirm` | `bool` | `false` | Required when any GSI `Delete` is present. |

**Returns:** `{"ok": true, "table": {...TableDescription...}}`

**Example — switch to on-demand billing:**

```
update_table(table_name="Users", billing_mode="PAY_PER_REQUEST")
```

**Example — delete a GSI (irreversible, requires confirm):**

```
update_table(
  table_name="Users",
  global_secondary_index_updates=[{"Delete": {"IndexName": "old-index"}}],
  confirm=true
)
```

---

#### `delete_table`

Deletes a table and all its data. Requires `confirm=true`. No undo.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table_name` | `string` | — | Table to delete. |
| `wait_until_deleted` | `bool` | `true` | Block until deletion completes. |
| `confirm` | `bool` | `false` | Must be `true` to execute. |

**Returns:** `{"ok": true, "table": {...TableDescription at time of deletion...}}`

**Example:**

```
# Preview
delete_table(table_name="Tasks")

# Execute
delete_table(table_name="Tasks", confirm=true)
```

---

## Output formats

All read tools accept `format: "json" | "table"`.

**`format="json"` (default):** Returns a structured dict. `rendered` is always `null`.

**`format="table"`:** Returns the same structured dict, with `rendered` set to a Rich-formatted fixed-width table string. Useful in terminals and direct transcripts.

```
# Example rendered table output
                    Query: Orders
┌──────────┬──────────────────────┬──────────┐
│ user_id  │ order_id             │ total    │
├──────────┼──────────────────────┼──────────┤
│ u_123    │ 2024-01-01-order-99  │ 42       │
│ u_123    │ 2024-01-15-order-07  │ 17       │
└──────────┴──────────────────────┴──────────┘
```

Nested dicts and lists are JSON-encoded into a single cell. Binary values display as `<N bytes>`.

---

## Pagination

`query` and `scan` follow multiple DynamoDB pages automatically. When a result is truncated, `last_evaluated_key` is non-null. Pass it back as `exclusive_start_key` to resume:

```
# First call
result = query(
  table_name="Orders",
  key_condition_expression="user_id = :u",
  expression_attribute_values={":u": "u_123"},
  max_pages=3
)

# If result["last_evaluated_key"] is not null:
next_page = query(
  table_name="Orders",
  key_condition_expression="user_id = :u",
  expression_attribute_values={":u": "u_123"},
  exclusive_start_key=result["last_evaluated_key"]
)
```

### Pagination controls

| Parameter | Description |
|---|---|
| `max_pages` | Maximum DynamoDB pages to follow per call. Default 5, hard cap 50. |
| `max_items` | Cap on total accumulated items across all pages. |
| `max_read_units` | Read-capacity budget. Pagination stops when `ConsumedCapacity.CapacityUnits` exceeds this value. |
| `limit` | DynamoDB page size (passed to each page request). Evaluated before `filter_expression`, so actual items per page can be lower. |

When `max_read_units` stops pagination early, `stopped_early_reason` in the response explains why.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No AWS credentials resolved` | Set `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`, or `AWS_PROFILE`, or run inside an environment with a boto3 default credential chain. |
| `AWS_REGION is required` | Add `"AWS_REGION": "us-east-1"` to your MCP client's `env` block. |
| `Could not connect to the endpoint URL` | For DynamoDB Local: confirm the container is running (`docker ps`) and `DYNAMODB_ENDPOINT_URL` matches the exposed port (`http://localhost:8000`). |
| `ExpiredTokenException` | Refresh your SSO session: `aws sso login --profile <profile>`, then restart your MCP client. |
| `ResourceNotFoundException` | The table doesn't exist in the active region. Run `list_tables` to confirm. |
| `ValidationException` on `query` | Ensure `key_condition_expression` references the partition key. Check attribute name casing. |
| `ValidationException` on PartiQL | DynamoDB PartiQL: partition key required in `WHERE`, single-quoted string literals, no JOINs, positional `?` placeholders. |
| `TableAccessDenied` error | The table is not in your `DYNAMODB_ALLOWED_TABLES`, or it is in `DYNAMODB_BLOCKED_TABLES`. Adjust the env var. |
| Tools not appearing in the client | Restart the MCP client after editing its config file. Check server stderr for startup errors. |
| `Unsupported MCP_TRANSPORT` | Only `stdio` is supported. Remove or unset `MCP_TRANSPORT`. |
| `ConditionalCheckFailedException` | The item may already exist (for `attribute_not_exists`) or was modified concurrently. Check your condition and retry. |

**Checking server stderr:** Most MCP hosts (Claude Desktop, Claude Code, Cursor) expose server stderr in their developer/debug panels. Startup errors always appear there with a human-readable message.

---

## Development

### Setup

```bash
git clone https://github.com/shreetishrestha/mcp-dynamo.git
cd mcp-dynamo
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Running tests

Tests use [moto](https://github.com/getmoto/moto) to mock AWS — no real credentials or running containers needed.

```bash
pytest                        # all tests
pytest tests/test_items.py    # single file
pytest -k "delete"            # filter by name
pytest -x                     # stop on first failure
pytest -v                     # verbose
```

Test fixtures are in `tests/conftest.py`. Every test gets two pre-seeded tables:
- `Users` — partition key `id (S)`
- `Orders` — partition key `user_id (S)`, sort key `order_id (S)`

### Linting

```bash
ruff check src tests    # lint
ruff format src tests   # auto-format
```

Line length is 100. Target is Python 3.11+.

### Running locally against DynamoDB Local

```bash
docker run -d -p 8000:8000 --name ddb-local amazon/dynamodb-local

export AWS_ACCESS_KEY_ID=local
export AWS_SECRET_ACCESS_KEY=local
export AWS_REGION=us-east-1
export DYNAMODB_ENDPOINT_URL=http://localhost:8000

python -m mcp_dynamo
```

Inspect interactively:

```bash
npx @modelcontextprotocol/inspector python -m mcp_dynamo
```

### Adding a new tool

1. **Implement the handler** in the relevant file under `src/mcp_dynamo/tools/`:

   ```python
   def register(mcp: FastMCP) -> None:
       @mcp.tool()
       def my_new_tool(table_name: str, ...) -> dict[str, Any]:
           """One-sentence summary. Full docstring for FastMCP schema generation."""
           if denied := check_table_access(table_name, get_config()):
               return denied
           # ... AWS call ...
   ```

   FastMCP infers the JSON schema from type annotations and the docstring automatically.

2. **If destructive**, add the `@requires_confirm` decorator inside `@mcp.tool()`:

   ```python
   @mcp.tool()
   @requires_confirm(action="my_new_tool", target_keys=("table_name",))
   def my_new_tool(table_name: str, confirm: bool = False) -> dict[str, Any]:
       ...
   ```

   `confirm` must be a declared parameter or the decorator raises `TypeError` at import time.

3. **Register a new file** (if you created one) in `src/mcp_dynamo/tools/__init__.py`:

   ```python
   from mcp_dynamo.tools import admin, discovery, items, my_module, partiql, queries

   def register_all(mcp: FastMCP) -> None:
       discovery.register(mcp)
       items.register(mcp)
       queries.register(mcp)
       partiql.register(mcp)
       admin.register(mcp)
       my_module.register(mcp)
   ```

4. **Write a test** in `tests/` using the `call` fixture from `conftest.py`:

   ```python
   async def test_my_new_tool(call, seed_tables):
       result = await call("my_new_tool", table_name="Users", ...)
       assert result["ok"] is True
   ```

---

## Architecture

```
src/mcp_dynamo/
  __main__.py      Entrypoint — calls server.run()
  server.py        FastMCP instance bootstrap, credential check, startup
  config.py        Env-var resolution, Config dataclass, ConfigError
  client.py        boto3 client factory (lru_cache), credential verification
  access_control.py  Allow/block list enforcement
  safety.py        @requires_confirm decorator, dry-run preview, statement_is_destructive
  errors.py        ClientError → plain-English dict translation
  formatting.py    to_json(), to_table() (Rich), render() helper
  tools/
    __init__.py    register_all() — wires every module into FastMCP
    discovery.py   dynamo_whoami, list_tables, describe_table, infer_schema
    items.py       get/put/update/delete item, batch get/write
    queries.py     query, scan (paginated, max_pages cap)
    partiql.py     execute_partiql_statement, execute_partiql_batch
    admin.py       create/update/delete table
```

**Key design decisions:**

- **Python-native types everywhere.** Tool parameters and return values use plain Python dicts, lists, strings, numbers. Serialization to DynamoDB's `{"S": "..."}` wire format happens inside `client.py` via the high-level boto3 Resource API. PartiQL parameters are serialized by `boto3.dynamodb.types.TypeSerializer`.
- **Single-process, lru_cache clients.** The boto3 client and resource are cached per-process. Safe for the single-threaded stdio event loop. Tests call `reset_clients()` between runs to get a clean state.
- **Startup validation.** `verify_credentials()` issues a real `list_tables(Limit=1)` call before any tool is registered. A bad config causes a hard exit with a human-readable message on stderr.
- **Stderr hygiene.** Only operational status (version, transport, table access mode) goes to stderr. Credentials, account IDs, and request bodies are never logged — MCP hosts capture and display stderr to end users.
- **Confirm guard.** The `@requires_confirm` decorator wraps the tool function *inside* `@mcp.tool()`. FastMCP sees the full signature (including `confirm`) and includes it in the JSON schema. If `confirm` is absent from the function signature, the decorator raises `TypeError` at import time so misconfiguration fails loudly.

---

## License

MIT
