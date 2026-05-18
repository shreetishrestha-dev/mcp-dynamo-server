# Plan: `mcp-dynamo` — A DynamoDB MCP Server

## Context

You want a Model Context Protocol (MCP) server that any LLM client (Claude Desktop, Claude Code, Cursor, etc.) can plug into to operate on a DynamoDB database — either local (DynamoDB Local) or hosted on AWS. The server should expose full CRUD, query/scan, batch ops, table admin, and a SQL-like surface, with optional tabular rendering of results. It should accept AWS credentials at startup, be installable in any MCP-capable project, and ship as a Docker image as well as a pip package.

The repo (`/Users/shreetishrestha/Dev/Experiments/mcp-dynamo`) is currently empty — this is a greenfield build.

**Decisions locked in (from clarifying questions):**
- **Language**: Python 3.11+, official `mcp` SDK, `boto3`
- **Safety model**: All ops always registered; destructive ops (`delete_item`, `delete_table`, `batch_write_item` deletes, `execute_partiql` DELETEs) require `confirm=true` in the tool call args, otherwise the tool returns a dry-run preview
- **Transport**: stdio only for v1
- **Scope**: CRUD + query/scan + batch + PartiQL + tabular formatting + table admin (create/update/delete tables, GSIs/LSIs)

---

## How "AWS creds at first" works in MCP

MCP stdio servers can't pop UI prompts — the host launches the server with env vars / args. So "asks for creds at first" translates to: **the MCP client config supplies credentials at launch time** via env vars. The server reads them on startup and fails fast with a clear error if missing/invalid. We will support, in priority order:

1. Explicit env vars: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`
2. `AWS_PROFILE` (reads `~/.aws/credentials`)
3. Default boto3 chain (IAM role, SSO, etc.)
4. `DYNAMODB_ENDPOINT_URL` override → points at DynamoDB Local (`http://localhost:8000`) instead of AWS

A `dynamo_whoami` tool will let the LLM verify which identity/region/endpoint is active.

---

## Project layout

```
mcp-dynamo/
├── pyproject.toml              # uv/pip metadata, declares `mcp-dynamo` entry point
├── README.md                   # install + MCP client config snippets
├── Dockerfile                  # python:3.12-slim base, stdio entry
├── .dockerignore
├── .env.example                # sample env vars
├── src/mcp_dynamo/
│   ├── __init__.py
│   ├── __main__.py             # `python -m mcp_dynamo` → server.run()
│   ├── server.py               # FastMCP server, registers all tools
│   ├── client.py               # boto3 client factory + cred resolution
│   ├── config.py               # env parsing, validation, ReadOnly/Admin gates
│   ├── formatting.py           # JSON ↔ ASCII table (uses `rich` or `tabulate`)
│   ├── safety.py               # confirm-flag enforcement, dry-run rendering
│   └── tools/
│       ├── __init__.py
│       ├── discovery.py        # list_tables, describe_table, dynamo_whoami
│       ├── items.py            # get/put/update/delete_item, batch_get/write
│       ├── queries.py          # query, scan (with pagination caps)
│       ├── partiql.py          # execute_partiql_statement, execute_partiql_batch
│       └── admin.py            # create/update/delete_table, GSI/LSI helpers
└── tests/
    ├── conftest.py             # spins up moto mock + sample tables
    ├── test_items.py
    ├── test_queries.py
    ├── test_partiql.py
    ├── test_admin.py
    └── test_safety.py          # verifies confirm-flag enforcement
```

---

## Tool surface (v1)

All tools accept an optional `format: "json" | "table"` param (default `json`). Table output uses `rich.table` rendered to a fixed-width string.

| Tool | Purpose | Destructive? |
|---|---|---|
| `dynamo_whoami` | Returns active AWS identity, region, endpoint | No |
| `list_tables` | List table names with optional prefix filter | No |
| `describe_table` | Schema, indexes, throughput, item count | No |
| `get_item` | Fetch by key | No |
| `put_item` | Insert/replace item | No (overwrites are explicit) |
| `update_item` | Patch via UpdateExpression | No |
| `delete_item` | Delete by key | **Yes — confirm required** |
| `query` | Indexed lookup with KeyConditionExpression | No |
| `scan` | Full table scan (capped pages, returns LastEvaluatedKey) | No |
| `batch_get_item` | Multi-key fetch | No |
| `batch_write_item` | Bulk put/delete | **Yes if any delete — confirm required** |
| `execute_partiql_statement` | Single PartiQL (SELECT/INSERT/UPDATE/DELETE) | **Yes if DELETE — confirm required** |
| `execute_partiql_batch` | Batch PartiQL statements | **Yes if any DELETE — confirm required** |
| `create_table` | Create with keys + GSIs/LSIs + billing mode | No |
| `update_table` | Add/remove GSIs, change throughput | No |
| `delete_table` | Drop table | **Yes — confirm required** |

**Confirm-flag pattern**: destructive tool called without `confirm=true` returns a structured dry-run preview:
```json
{
  "dry_run": true,
  "action": "delete_item",
  "target": {"table": "Users", "key": {"id": "u_123"}},
  "message": "Re-call with confirm=true to execute."
}
```

This sits in `src/mcp_dynamo/safety.py` as a decorator `@requires_confirm` applied to destructive tool handlers.

---

## Critical files to create (in implementation order)

1. **`pyproject.toml`** — declare deps (`mcp`, `boto3`, `rich`), entry point `mcp-dynamo = "mcp_dynamo.__main__:main"`
2. **`src/mcp_dynamo/config.py`** — single source of truth for env vars; raises a clear error if creds missing
3. **`src/mcp_dynamo/client.py`** — `get_dynamodb_client()` and `get_dynamodb_resource()` using config
4. **`src/mcp_dynamo/server.py`** — instantiate FastMCP, import & register tool modules
5. **`src/mcp_dynamo/safety.py`** — `@requires_confirm` decorator
6. **`src/mcp_dynamo/formatting.py`** — `to_json()` and `to_table()` helpers
7. **`src/mcp_dynamo/tools/discovery.py`** — start here, simplest read-only tools to validate the wiring
8. **`src/mcp_dynamo/tools/items.py`** — single-item CRUD
9. **`src/mcp_dynamo/tools/queries.py`** — query/scan with pagination caps
10. **`src/mcp_dynamo/tools/partiql.py`** — wraps `execute_statement`/`batch_execute_statement`
11. **`src/mcp_dynamo/tools/admin.py`** — table lifecycle
12. **`tests/conftest.py`** — `moto.mock_aws` fixture, creates `Users` + `Orders` sample tables
13. **`Dockerfile`** — multi-stage; final stage runs `python -m mcp_dynamo`
14. **`README.md`** — install methods + MCP client config snippets for Claude Desktop / Claude Code / Cursor

---

## Key implementation details

**boto3 client factory** (`client.py`):
```python
session = boto3.Session(
    aws_access_key_id=cfg.access_key,
    aws_secret_access_key=cfg.secret_key,
    aws_session_token=cfg.session_token,
    region_name=cfg.region,
    profile_name=cfg.profile,
)
kwargs = {"endpoint_url": cfg.endpoint_url} if cfg.endpoint_url else {}
return session.client("dynamodb", **kwargs)
```

**PartiQL is native** — `dynamodb.execute_statement(Statement="SELECT * FROM Users WHERE id = 'u_123'")`. No translator needed; the LLM can write PartiQL directly, and we add a tool description that documents the dialect's quirks (PK/SK required in WHERE for non-scans, no JOINs, etc.).

**Pagination caps** — `scan` and `query` accept `max_pages` (default 5) and return `LastEvaluatedKey` so the LLM can resume. Prevents accidental full-table scans on big tables from blowing up the LLM context.

**Tabular formatting** — `rich.table.Table` rendered via `Console(file=io.StringIO(), width=120).print(table)`. For nested values (maps/lists), JSON-encode the cell so the table stays readable.

**Tool descriptions matter** — each tool's docstring becomes its MCP description. Write these carefully with examples since the LLM uses them to decide which tool to call.

---

## MCP client config snippets (for README)

**Claude Desktop / Claude Code** (`~/.claude/claude_desktop_config.json` or `~/.claude.json`):
```json
{
  "mcpServers": {
    "dynamodb": {
      "command": "uvx",
      "args": ["mcp-dynamo"],
      "env": {
        "AWS_ACCESS_KEY_ID": "...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

**Local DynamoDB**:
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

**Docker**:
```json
{
  "command": "docker",
  "args": ["run", "-i", "--rm",
           "-e", "AWS_ACCESS_KEY_ID",
           "-e", "AWS_SECRET_ACCESS_KEY",
           "-e", "AWS_REGION",
           "mcp-dynamo:latest"]
}
```

---

## Verification

1. **Unit / integration tests** (`uv run pytest`): moto-backed, must pass for all tools including the confirm-flag enforcement in `test_safety.py`.
2. **Local DynamoDB smoke test**: 
   ```bash
   docker run -d -p 8000:8000 amazon/dynamodb-local
   DYNAMODB_ENDPOINT_URL=http://localhost:8000 AWS_REGION=us-east-1 \
     AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local \
     uv run mcp-dynamo
   ```
   Then connect via the MCP Inspector (`npx @modelcontextprotocol/inspector`) and exercise each tool.
3. **End-to-end via Claude Code**: add the server to `~/.claude.json` (config above), open a session, ask Claude to: list tables → create a `Tasks` table → insert rows → run a PartiQL SELECT → render results as a table → try a delete without `confirm` (should dry-run) → delete with `confirm`.
4. **Docker image**: `docker build -t mcp-dynamo .` then run the snippet above and verify Claude Code can connect.

---

## Out of scope for v1 (parking lot)

- HTTP/SSE transport (stdio-only for now)
- DynamoDB Streams, TTL config, backups, PITR
- Auto-generated "conversational query → PartiQL" tool (the calling LLM already does this naturally — no need to embed a second LLM in the server)
- Cross-region replication / Global Tables admin
- IAM policy generation
