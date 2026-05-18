# mcp-dynamo — Claude Code guide

## Quick orientation

MCP server that exposes Amazon DynamoDB operations to LLM clients over stdio. Built with FastMCP (`mcp` package). Supports both AWS-hosted DynamoDB and DynamoDB Local.

```
src/mcp_dynamo/
  __main__.py      entrypoint — calls server.run()
  server.py        FastMCP instance, credential check, startup
  config.py        env-var resolution, Config dataclass, ConfigError
  client.py        boto3 DynamoDB client factory + credential verification
  safety.py        confirm=true guard for destructive operations
  formatting.py    Rich table renderer for format="table" responses
  tools/
    __init__.py    register_all() — wires every tool module into FastMCP
    items.py       get/put/update/delete item, batch get/write
    queries.py     query, scan (paginated, max_pages cap)
    partiql.py     execute_partiql_statement, execute_partiql_batch
    admin.py       create/update/delete/describe/list table
    discovery.py   dynamo_whoami
```

## Development setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

Tests use `moto` to mock AWS — no real credentials needed.

```bash
pytest                  # all tests
pytest tests/test_items.py          # single file
pytest -k "delete"      # by keyword
pytest -x               # stop on first failure
```

## Linting

```bash
ruff check src tests    # lint
ruff format src tests   # format
```

Line length is 100. Target is Python 3.11+.

## Running the server locally

Against DynamoDB Local (easiest for dev):

```bash
docker run -d -p 8000:8000 --name ddb-local amazon/dynamodb-local

export AWS_ACCESS_KEY_ID=local
export AWS_SECRET_ACCESS_KEY=local
export AWS_REGION=us-east-1
export DYNAMODB_ENDPOINT_URL=http://localhost:8000

python -m mcp_dynamo
```

Inspect with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python -m mcp_dynamo
```

## Key conventions

- **Tools use Python-native dicts** for keys/items. Serialization to DynamoDB wire format (`{"S": "..."}`, etc.) happens inside `client.py`, not in tool handlers.
- **Safety guard** (`safety.py`): any tool flagged destructive calls `require_confirm()` before executing. Returns a dry-run JSON blob if `confirm` is falsy; callers never see partial execution.
- **Pagination**: `query` and `scan` cap at `max_pages` (default 5). They return `last_evaluated_key` so the caller can resume.
- **format param**: read tools accept `format: "json" | "table"`. `"table"` uses Rich to render a human-readable table. Default is `"json"`.
- **Startup validation**: `server.run()` calls `verify_credentials()` before registering tools. Hard exit on `ConfigError` — the MCP client shows stderr, so the message must be actionable without leaking secrets.
- **stderr policy**: only log operational status (version, transport). Never log credentials, account IDs, or request bodies.

## Adding a new tool

1. Add the handler function in the relevant file under `tools/`.
2. Decorate with `@mcp.tool()` — FastMCP infers the JSON schema from type annotations and docstring.
3. If the tool is destructive, call `require_confirm(action, target, confirm)` before any AWS call.
4. Register the new module (if new file) in `tools/__init__.py` inside `register_all()`.
5. Add a test in `tests/` using the `moto` fixtures from `tests/conftest.py`.

## Transport

Only `stdio` is supported. `MCP_TRANSPORT` env var is read at startup; anything other than `"stdio"` exits with code 2.

## Dependencies

- `mcp>=1.0.0` — FastMCP framework
- `boto3>=1.34.0` — AWS SDK
- `rich>=13.7.0` — table rendering
- dev: `pytest`, `pytest-asyncio`, `moto[dynamodb]`, `ruff`
