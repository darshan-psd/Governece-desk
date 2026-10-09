"""Helpers that reproduce the JavaScript value semantics the original Node backend relied on.

The ported business rules depend on JS behaviour such as truthiness (``[]`` is truthy),
``String(value)`` conversion and ``Number(value)`` coercion. Keeping these rules in one
place lets the services read like the original rules without subtle behaviour changes.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def truthy(value: Any) -> bool:
    """JavaScript truthiness: empty lists and dicts are truthy, NaN is falsy."""
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


def js_string(value: Any) -> str:
    """``String(value)`` for JSON-compatible values."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return str(int(value)) if value.is_integer() and abs(value) < 1e21 else repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    return "[object Object]"


def text(value: Any, max_length: int = 500) -> str:
    """``String(v ?? '').trim().slice(0, max)`` from the original rules."""
    return ("" if value is None else js_string(value)).strip()[:max_length]


def as_list(value: Any) -> list:
    """``Array.isArray(v) ? v : []``."""
    return value if isinstance(value, list) else []


def prop(value: Any, key: str) -> Any:
    """Optional property access (``value?.key``) that tolerates non-objects."""
    return value.get(key) if isinstance(value, dict) else None


def js_number(value: Any) -> float | int:
    """``Number(value)``. Integral results are returned as ``int`` so JSON output matches JS."""
    if value is None:
        result: float = 0.0
    elif isinstance(value, bool):
        return 1 if value else 0
    elif isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            result = 0.0
        elif _NUMBER_RE.match(stripped):
            result = float(stripped)
        else:
            return math.nan
    else:
        return math.nan
    if math.isfinite(result) and result.is_integer():
        return int(result)
    return result


def is_integer(value: Any) -> bool:
    """``Number.isInteger(value)``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def is_safe_integer(value: Any) -> bool:
    return is_integer(value) and abs(value) <= MAX_SAFE_INTEGER


def lt(a: Any, b: Any) -> bool:
    """``a < b`` for the date/timestamp strings compared by the rules; undefined compares false."""
    return isinstance(a, str) and isinstance(b, str) and a < b


def le(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a <= b


def gt(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a > b


def unique(values: list) -> list:
    """Order-preserving ``[...new Set(values)]``."""
    seen: list = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def now_iso() -> str:
    """``new Date().toISOString()`` (millisecond precision, ``Z`` suffix)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def today() -> str:
    return now_iso()[:10]


def date_valid(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, re.ASCII):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False
