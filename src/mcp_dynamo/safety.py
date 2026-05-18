"""Confirm-flag enforcement for destructive tools.

Destructive tools never run on the first call. They look at a `confirm: bool`
kwarg — if `False` (or missing), they return a structured dry-run preview so the
LLM can re-issue the call with `confirm=true`.

This is enforced at the tool layer via the `@requires_confirm` decorator, which
builds a target dict from the wrapped function's bound arguments (filtered by
`target_keys`) and emits the preview.
"""

from __future__ import annotations

import functools
import inspect
import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any

# Recognizes PartiQL statements whose top-level verb is DELETE / UPDATE / REMOVE
# even when wrapped in leading SQL line comments, block comments, parens, or
# arbitrary whitespace. The input is NFKC-normalized first to defeat
# zero-width characters and other Unicode obfuscation tricks.
_DESTRUCTIVE = re.compile(
    r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\(|\s)*(delete|update|remove)\b",
    re.IGNORECASE | re.DOTALL,
)


def build_dry_run(action: str, target: dict[str, Any], message: str | None = None) -> dict[str, Any]:
    """Return the canonical dry-run preview payload.

    ``target`` is run through ``to_json`` so values like ``Decimal``, ``bytes``,
    ``Binary``, ``datetime``, and ``set`` don't crash the preview path. Imported
    locally to avoid a circular import (formatting may import from safety in
    the future).
    """
    from mcp_dynamo.formatting import to_json

    return {
        "dry_run": True,
        "action": action,
        "target": to_json(target),
        "message": message or "Re-call with confirm=true to execute.",
    }


# IMPORTANT: apply @requires_confirm INSIDE @mcp.tool() — i.e. the @mcp.tool()
# decorator must be on top so FastMCP sees the (confirm-aware) wrapped signature.
def requires_confirm(
    action: str,
    target_keys: Iterable[str] = (),
    *,
    is_destructive: Callable[[dict[str, Any]], bool] | None = None,
):
    """Decorator: short-circuit destructive tool calls without `confirm=true`.

    Parameters
    ----------
    action:
        Tool name as it appears in the dry-run payload (e.g. ``"delete_item"``).
    target_keys:
        Argument names whose values should be copied into the dry-run ``target``
        dict so the LLM can see what would have been touched.
    is_destructive:
        Optional predicate over the bound arguments. If supplied and it returns
        ``False`` for the current call, the decorator is a no-op (the call runs
        directly). Used by ``batch_write_item`` and ``execute_partiql_*`` where
        only some shapes are destructive.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(func)
        if "confirm" not in sig.parameters:
            raise TypeError(
                f"@requires_confirm({action!r}) applied to {func.__name__!r} "
                "but its signature does not declare a 'confirm' parameter."
            )

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            args_dict = dict(bound.arguments)

            if is_destructive is not None and not is_destructive(args_dict):
                return func(*args, **kwargs)

            confirm = bool(args_dict.get("confirm", False))
            if confirm:
                return func(*args, **kwargs)

            target = {k: args_dict[k] for k in target_keys if k in args_dict}
            return build_dry_run(action=action, target=target)

        return wrapper

    return decorator


def statement_is_destructive(statement: str) -> bool:
    """Heuristic: does a PartiQL or UpdateExpression statement mutate destructively?

    Treats DELETE, UPDATE, and REMOVE as destructive verbs. Two normalization
    steps are applied before matching:
    1. NFKC — collapses compatibility variants (full-width chars, etc.)
    2. Strip Unicode format characters (category ``Cf``) — removes zero-width
       joiners/non-joiners, BOM, and other invisible glyphs that could split a
       keyword like ``DELETE`` across codepoints.
    """
    if not statement:
        return False
    norm = "".join(
        c for c in unicodedata.normalize("NFKC", statement)
        if unicodedata.category(c) != "Cf"
    )
    return bool(_DESTRUCTIVE.match(norm))
