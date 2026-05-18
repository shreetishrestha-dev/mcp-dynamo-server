"""Entry point for `python -m mcp_dynamo` and the `mcp-dynamo` console script."""

from __future__ import annotations

from mcp_dynamo.server import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
