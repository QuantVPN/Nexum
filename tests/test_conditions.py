import pytest

from nexum.automation.conditions import check, evaluate, resolve_path, validate
from nexum.errors import ValidationError

CTX = {
    "event": {"is_open": True, "hours": 9, "employee_id": None, "kind": "vacation"},
    "now": {"weekday": 1, "hour": 10, "date": "2026-09-08"},
    "employee": {"full_name": "Eva Lund"},
}


def test_resolve_path_handles_missing_and_nested() -> None:
    assert resolve_path(CTX, "event.hours") == 9
    assert resolve_path(CTX, "event.missing") is None
    assert resolve_path(CTX, "nope.deeper.x") is None
    assert resolve_path({"a": [1, 2]}, "a.1") == 2
    assert resolve_path({"a": [1, 2]}, "a.9") is None


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ({"field": "event.is_open", "op": "is_true"}, True),
        ({"field": "event.is_open", "op": "is_false"}, False),
        ({"field": "event.employee_id", "op": "is_null"}, True),
        ({"field": "event.employee_id", "op": "not_null"}, False),
        ({"field": "event.hours", "op": "gt", "value": 8}, True),
        ({"field": "event.hours", "op": "gte", "value": 9}, True),
        ({"field": "event.hours", "op": "lt", "value": 9}, False),
        ({"field": "event.hours", "op": "lte", "value": "9"}, True),
        ({"field": "event.kind", "op": "in", "value": ["vacation", "sick"]}, True),
        ({"field": "event.kind", "op": "not_in", "value": ["sick"]}, True),
        ({"field": "employee.full_name", "op": "contains", "value": "Lund"}, True),
        ({"field": "employee.full_name", "op": "startswith", "value": "Eva"}, True),
        ({"field": "event.kind", "op": "ne", "value": "sick"}, True),
        ({"field": "now.date", "op": "gt", "value": "2026-09-01"}, True),
        ({"field": "event.missing", "op": "gt", "value": 1}, False),
        (
            {
                "any": [
                    {"field": "event.hours", "op": "gt", "value": 100},
                    {"field": "now.weekday", "op": "eq", "value": 1},
                ]
            },
            True,
        ),
        ({"not": {"field": "event.is_open", "op": "is_true"}}, False),
        (
            {
                "all": [
                    {"field": "event.is_open", "op": "is_true"},
                    {"field": "now.hour", "op": "eq", "value": 10},
                ]
            },
            True,
        ),
    ],
)
def test_check(condition: dict, expected: bool) -> None:
    assert check(condition, CTX) is expected


def test_evaluate_is_conjunction() -> None:
    assert evaluate([], CTX) is True
    assert evaluate(None, CTX) is True
    assert evaluate(
        [
            {"field": "event.is_open", "op": "is_true"},
            {"field": "now.hour", "op": "eq", "value": 10},
        ],
        CTX,
    )
    assert not evaluate(
        [
            {"field": "event.is_open", "op": "is_true"},
            {"field": "now.hour", "op": "eq", "value": 11},
        ],
        CTX,
    )


def test_validate_rejects_bad_shapes() -> None:
    with pytest.raises(ValidationError):
        validate("not a list")
    with pytest.raises(ValidationError):
        validate([{"field": "x", "op": "explode"}])
    with pytest.raises(ValidationError):
        validate([{"op": "eq"}])
    assert validate(None) == []
    assert validate([{"any": [{"field": "a", "op": "eq", "value": 1}]}])


def test_type_mismatch_is_false_not_error() -> None:
    assert check({"field": "employee.full_name", "op": "gt", "value": 3}, CTX) is False
