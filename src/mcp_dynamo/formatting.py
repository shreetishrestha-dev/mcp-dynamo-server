"""JSON / table rendering helpers used by every read-shaped tool.

`to_json` produces a compact-but-readable dict suitable for direct return from
an MCP tool. `to_table` renders a list of row-dicts as a fixed-width Rich table
string so the LLM (and a human reading the transcript) can scan results.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
from decimal import Decimal
from typing import Any, Literal

from boto3.dynamodb.types import Binary
from rich.console import Console
from rich.table import Table

FormatLiteral = Literal["json", "table"]


class _DDBEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal, bytes, Binary, set, datetime.

    Used as a backstop for any value that slips past ``to_json``'s recursive
    walker (e.g. nested inside a custom object). The walker is the primary path
    because it handles tuples and sets without losing information.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, Binary):
            return {"$binary": o.value.hex()}
        if isinstance(o, Decimal):
            # Integer-valued Decimals → int, else float for readability.
            if o == o.to_integral_value():
                return int(o)
            return float(o)
        if isinstance(o, bytes | bytearray):
            return {"$binary": o.hex()}
        if isinstance(o, set | frozenset):
            return sorted(o, key=str)
        if isinstance(o, _dt.datetime | _dt.date):
            return o.isoformat()
        return super().default(o)


def _convert(value: Any) -> Any:
    """Recursively convert DynamoDB-flavored Python values to plain JSON-safe types.

    Handles in one pass: Decimal, Binary, bytes/bytearray, set/frozenset,
    tuple, datetime/date, and walks dicts/lists. Anything else is returned
    as-is and will hit the encoder's ``default`` if it ever gets JSON-dumped.
    """
    if isinstance(value, Binary):
        return {"$binary": value.value.hex()}
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, bytes | bytearray):
        return {"$binary": value.hex()}
    if isinstance(value, _dt.datetime | _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _convert(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(v) for v in value]
    if isinstance(value, tuple):
        # Preserve tuple→list (JSON has no tuple); avoid the silent json round-trip.
        return [_convert(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted((_convert(v) for v in value), key=str)
    return value


def to_json(value: Any) -> Any:
    """Convert ``value`` into plain JSON-safe Python types.

    A recursive walker (rather than ``json.loads(json.dumps(...))``) so we
    don't lose information (e.g. tuples were already lists; sets are sorted
    for deterministic ordering) and we don't crash on non-JSON-serializable
    DynamoDB types like ``Binary``.
    """
    return _convert(value)


def _stringify_cell(value: Any) -> str:
    if isinstance(value, Binary):
        return f"<{len(value.value)} bytes>"
    if isinstance(value, dict | list | set | tuple):
        return json.dumps(to_json(value), separators=(", ", ": "))
    if isinstance(value, Decimal):
        return str(to_json(value))
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if value is None:
        return ""
    return str(value)


def to_table(
    rows: list[dict[str, Any]],
    *,
    title: str | None = None,
    width: int = 120,
    max_rows: int | None = None,
) -> str:
    """Render ``rows`` as a Rich table, returning the rendered string.

    Columns are the union of keys across rows in first-seen order. Nested
    values are JSON-encoded so the table stays one row per item.

    Parameters
    ----------
    rows:
        List of row-dicts. Empty list returns ``"(no rows)"``.
    title:
        Optional table title (shown above the table by Rich).
    width:
        Console width to render at. Defaults to 120.
    max_rows:
        If provided and ``len(rows) > max_rows``, only the first ``max_rows``
        are rendered and a ``... (N more rows)`` footer is appended.
    """
    if not rows:
        return "(no rows)"

    display_rows = rows
    truncated = 0
    if max_rows is not None and len(rows) > max_rows:
        display_rows = rows[:max_rows]
        truncated = len(rows) - max_rows

    columns: list[str] = []
    seen: set[str] = set()
    for row in display_rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    table = Table(title=title, show_lines=False)
    for col in columns:
        table.add_column(col, overflow="fold")
    for row in display_rows:
        table.add_row(*[_stringify_cell(row.get(col)) for col in columns])

    buffer = io.StringIO()
    Console(file=buffer, width=width, record=False, force_terminal=False).print(table)
    rendered = buffer.getvalue().rstrip()
    if truncated:
        rendered = f"{rendered}\n... ({truncated} more rows)"
    return rendered


def render(payload: Any, fmt: FormatLiteral, *, rows_key: str | None = None,
           title: str | None = None) -> Any:
    """Top-level helper: pick JSON or table output for a tool response.

    If ``fmt == "table"`` and ``rows_key`` is given, the named key in ``payload``
    is rendered as a table and re-attached as a ``"rendered"`` string while the
    structured data is preserved. If ``rows_key`` is omitted, ``payload`` itself
    must be a list of dicts.
    """
    payload = to_json(payload)
    if fmt != "table":
        return payload

    if rows_key is None:
        if not isinstance(payload, list):
            return payload
        return {"rendered": to_table(payload, title=title), "data": payload}

    rows = payload.get(rows_key) if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return payload
    rendered = to_table(rows, title=title)
    out = dict(payload)
    out["rendered"] = rendered
    return out
