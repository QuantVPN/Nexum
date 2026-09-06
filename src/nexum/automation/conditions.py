"""A tiny, safe condition language (no ``eval``).

A rule's ``conditions`` is a list that must *all* hold. Each item is either::

    {"field": "event.is_open", "op": "eq", "value": true}
    {"any": [ {...}, {...} ]}          # at least one of the nested conditions holds
    {"not": {...}}                     # negation

Fields are dotted paths into the run context (``event.*``, ``now.hour``, ``now.weekday``,
``employee.*`` and so on). Missing paths resolve to ``None``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from nexum.errors import ValidationError

OPERATORS: tuple[str, ...] = (
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "contains",
    "startswith",
    "is_null",
    "not_null",
    "is_true",
    "is_false",
)


def resolve_path(context: Mapping[str, Any], path: str) -> Any:
    current: Any = context
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list | tuple):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            current = getattr(current, part, None)
        if current is None:
            return None
    return current


def _comparable(left: Any, right: Any) -> tuple[Any, Any]:
    """Coerce common mismatches (ISO strings vs dates, numeric strings) before comparing."""
    if isinstance(left, str) and isinstance(right, datetime | date):
        with contextlib.suppress(ValueError):
            left = (
                datetime.fromisoformat(left)
                if isinstance(right, datetime)
                else date.fromisoformat(left)
            )
    if isinstance(right, str) and isinstance(left, datetime | date):
        with contextlib.suppress(ValueError):
            right = (
                datetime.fromisoformat(right)
                if isinstance(left, datetime)
                else date.fromisoformat(right)
            )
    if isinstance(left, int | float) and isinstance(right, str):
        with contextlib.suppress(ValueError):
            right = float(right)
    if isinstance(right, int | float) and isinstance(left, str):
        with contextlib.suppress(ValueError):
            left = float(left)
    return left, right


def check(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    if "any" in condition:
        return any(check(c, context) for c in condition["any"])
    if "not" in condition:
        return not check(condition["not"], context)
    if "all" in condition:
        return all(check(c, context) for c in condition["all"])

    field = condition.get("field")
    op = condition.get("op", "eq")
    if not isinstance(field, str) or op not in OPERATORS:
        raise ValidationError(f"Invalid condition: {dict(condition)!r}")
    actual = resolve_path(context, field)
    expected = condition.get("value")

    if op == "is_null":
        return actual is None
    if op == "not_null":
        return actual is not None
    if op == "is_true":
        return bool(actual) is True
    if op == "is_false":
        return bool(actual) is False
    if op == "in":
        return actual in (expected or [])
    if op == "not_in":
        return actual not in (expected or [])
    if op == "contains":
        return actual is not None and expected in actual
    if op == "startswith":
        return isinstance(actual, str) and isinstance(expected, str) and actual.startswith(expected)

    actual, expected = _comparable(actual, expected)
    try:
        if op == "eq":
            return bool(actual == expected)
        if op == "ne":
            return bool(actual != expected)
        if actual is None or expected is None:
            return False
        if op == "gt":
            return bool(actual > expected)
        if op == "gte":
            return bool(actual >= expected)
        if op == "lt":
            return bool(actual < expected)
        if op == "lte":
            return bool(actual <= expected)
    except TypeError:
        return False
    return False  # pragma: no cover - all operators handled above


def evaluate(conditions: Sequence[Mapping[str, Any]] | None, context: Mapping[str, Any]) -> bool:
    if not conditions:
        return True
    return all(check(c, context) for c in conditions)


def validate(conditions: Any) -> list[dict[str, Any]]:
    """Validate the shape of a conditions list; raises ValidationError."""
    if conditions is None:
        return []
    if not isinstance(conditions, list):
        raise ValidationError("conditions must be a list")
    for cond in conditions:
        if not isinstance(cond, dict):
            raise ValidationError("each condition must be an object")
        if "any" in cond or "all" in cond:
            key = "any" if "any" in cond else "all"
            validate(cond[key])
        elif "not" in cond:
            validate([cond["not"]])
        else:
            check(cond, {})  # raises on bad field/op
    return conditions
