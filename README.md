# mcp-dynamo

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that exposes Amazon DynamoDB operations to LLM clients — Claude Desktop, Claude Code, Cursor, Windsurf, and any other MCP-compatible host — over stdio.

Works against both AWS-hosted DynamoDB and DynamoDB Local. Ships as a pip package and a Docker image.

---

## Features

- Full CRUD: `get_item`, `put_item`, `update_item`, `delete_item`
- Paginated `query` and `scan` with `LastEvaluatedKey` resume tokens
- Batch operations: `batch_get_item`, `batch_write_item`
- PartiQL: `execute_partiql_statement`, `execute_partiql_batch`
- Table admin: `create_table`, `update_table`, `delete_table`, `describe_table`, `list_tables`
- Identity check: `dynamo_whoami`
- Safety: destructive ops require `confirm=true` or return a dry-run preview
- Optional `format: "table"` for Rich-rendered human-readable output

---

## Prerequisites

- Python 3.11+ **or** Docker (for running the server)
- AWS credentials (access key pair, SSO session, IAM role, or profile) **or** DynamoDB Local

---

## Installation

### Option A — uvx (recommended, zero install)

[`uvx`](https://docs.astral.sh/uv/) runs the package in an isolated env without a permanent install. Install `uv` once, then use `uvx` as the MCP command:

```bash
# install uv (if you haven't already)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

No further steps needed — your MCP client config will invoke `uvx mcp-dynamo` directly.

### Option B — pip

```bash
pip install mcp-dynamo
```

Verify:

```bash
mcp-dynamo --help
```

### Option C — pipx

```bash
pipx install mcp-dynamo
```

### Option D — Docker

```bash
docker pull shreetishrestha977/mcp-dynamo:latest
```

Or build from source:

```bash
git clone https://github.com/shreetishrestha-dev/mcp-dynamo-server.git
cd mcp-dynamo-server
docker build -t mcp-dynamo .
```

### Option E — development install (editable)

```bash
git clone https://github.com/shreetishrestha-dev/mcp-dynamo-server.git
cd mcp-dynamo-server
python3.13 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

---

## AWS credentials

| Variable | Required | Notes |
|---|---|---|
| `AWS_REGION` | **Yes** | e.g. `us-east-1`. Also accepts `AWS_DEFAULT_REGION`. |
| `AWS_ACCESS_KEY_ID` | One of | Static credentials. |
| `AWS_SECRET_ACCESS_KEY` | One of | Pair with the access key above. |
| `AWS_SESSION_TOKEN` | No | For temporary STS / SSO credentials. |
| `AWS_PROFILE` | One of | Named profile in `~/.aws/credentials` or `~/.aws/config`. |
| `DYNAMODB_ENDPOINT_URL` | No | Override endpoint, e.g. `http://localhost:8000` for DynamoDB Local. |

**Resolution order:**

1. `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (+ optional `AWS_SESSION_TOKEN`)
2. `AWS_PROFILE`
3. boto3 default chain (IAM instance role, ECS task role, SSO session, container creds, etc.)

The server fails fast at startup with an actionable error if no credentials resolve.

### AWS SSO (IAM Identity Center)

```bash
# one-time setup
aws configure sso

# refresh session before starting the server
aws sso login --profile my-sso-profile

# then set in your MCP env block:
# "AWS_PROFILE": "my-sso-profile"
```

---

## MCP client configuration

### Claude Desktop

Config file: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).

**With uvx + static credentials:**

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

**With an AWS profile:**

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

After editing the config, **restart Claude Desktop** for changes to take effect.

---

### Claude Code (CLI)

Add to `~/.claude.json` under `mcpServers`:

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

Or add it via the CLI:

```bash
claude mcp add dynamodb uvx mcp-dynamo \
  -e AWS_ACCESS_KEY_ID=AKIA... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -e AWS_REGION=us-east-1
```

---

### Cursor

Open **Settings → MCP** (or `~/.cursor/mcp.json`) and add:

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

Open **Settings → Cascade → MCP Servers** and add the same `mcpServers` block above.

---

### VS Code (Copilot Chat / GitHub Copilot with MCP support)

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

Replace the `uvx` command with `docker run`:

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

Run DynamoDB Local in Docker, then point the server at it:

```bash
docker run -d -p 8000:8000 --name ddb-local amazon/dynamodb-local
```

MCP client config:

```json
{
  "mcpServers": {
    "dynamodb": {
      "command": "uvx",
      "args": ["mcp-dynamo"],
      "env": {
        "AWS_ACCESS_KEY_ID": "local",
        "AWS_SECRET_ACCESS_KEY": "local",
        "AWS_REGION": "us-east-1",
        "DYNAMODB_ENDPOINT_URL": "http://localhost:8000"
      }
    }
  }
}
```

Any non-empty values work for the credentials when using DynamoDB Local.

---

## Verify the server is working

Use the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) to test the server without a full client:

```bash
npx @modelcontextprotocol/inspector uvx mcp-dynamo
```

Or with a local install:

```bash
npx @modelcontextprotocol/inspector python -m mcp_dynamo
```

The Inspector opens a browser UI where you can call tools interactively.

---

## Safety model

Destructive tools require an explicit `confirm=true` argument. Without it they return a dry-run preview instead of executing:

```json
{
  "dry_run": true,
  "action": "delete_item",
  "target": {"table": "Users", "key": {"id": "u_123"}},
  "message": "Re-call with confirm=true to execute."
}
```

This prevents an LLM from silently dropping rows or tables.

**Destructive tools:** `delete_item`, `delete_table`, `batch_write_item` (when any `DeleteRequest` is present), `execute_partiql_statement` / `execute_partiql_batch` (when any DELETE statement is present).

---

## Tool reference

| Tool | Category | Destructive |
|---|---|---|
| `dynamo_whoami` | Discovery | No |
| `list_tables` | Admin | No |
| `describe_table` | Admin | No |
| `create_table` | Admin | No |
| `update_table` | Admin | No |
| `delete_table` | Admin | **Yes** |
| `get_item` | Items | No |
| `put_item` | Items | No |
| `update_item` | Items | No |
| `delete_item` | Items | **Yes** |
| `query` | Query | No |
| `scan` | Query | No |
| `batch_get_item` | Batch | No |
| `batch_write_item` | Batch | **Yes** (if any delete) |
| `execute_partiql_statement` | PartiQL | **Yes** (if DELETE) |
| `execute_partiql_batch` | PartiQL | **Yes** (if any DELETE) |

All read tools accept `format: "json" | "table"` (default `json`).

---

## Troubleshooting

| Error | Fix |
|---|---|
| `No AWS credentials resolved` | Set `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY`, or `AWS_PROFILE`, or run in an environment with a default boto3 chain (IAM role, SSO session, etc.). |
| `AWS_REGION is required` | Add `"AWS_REGION": "us-east-1"` to the `env` block in your MCP config. |
| `Could not connect to the endpoint URL` | For DynamoDB Local, confirm the container is running (`docker ps`) and `DYNAMODB_ENDPOINT_URL` matches the exposed port. |
| `ResourceNotFoundException` | The table doesn't exist in the target region. Check with `list_tables`. |
| `ValidationException` on PartiQL | DynamoDB PartiQL requires the partition key in `WHERE` for non-scan reads, no `JOIN`s, and quoted string literals. |
| Tools not appearing in client | Restart the MCP client after editing its config file. |
| `ExpiredTokenException` | Refresh your SSO session: `aws sso login --profile <profile>`. |

---

## Development

```bash
git clone https://github.com/shreetishrestha-dev/mcp-dynamo-server.git
cd mcp-dynamo-server
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Lint and format:

```bash
ruff check src tests
ruff format src tests
```

See [CLAUDE.md](CLAUDE.md) for codebase conventions and architecture notes.

---

## License

MIT
