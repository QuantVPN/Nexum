"""Shared column types and helpers."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime for naive (assumed UTC) or aware input."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC in the database, always yields aware UTC datetimes in Python.

    SQLite discards tzinfo; PostgreSQL would keep it. Normalising on both sides means
    the rest of the code base can rely on aware datetimes everywhere.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return as_utc(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return as_utc(value)


def str_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Portable string-backed enum column (no native DB enum types)."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class StrEnum(enum.StrEnum):
    @classmethod
    def choices(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def coerce(cls, value: Any) -> StrEnum:
        if isinstance(value, cls):
            return value
        return cls(value)


class FixedPoint(TypeDecorator[Decimal]):
    """Exact decimal storage as a scaled integer (works identically on SQLite and PostgreSQL)."""

    impl = BigInteger
    cache_ok = True

    def __init__(self, scale: int = 2) -> None:
        super().__init__()
        self.scale = scale
        self._quantum = Decimal(1).scaleb(-scale)

    def process_bind_param(
        self, value: Decimal | float | int | str | None, dialect: Dialect
    ) -> int | None:
        if value is None:
            return None
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        return int(dec.quantize(self._quantum, rounding=ROUND_HALF_UP).scaleb(self.scale))

    def process_result_value(self, value: int | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value).scaleb(-self.scale).quantize(self._quantum)


def Money() -> FixedPoint:
    return FixedPoint(2)


def Hours() -> FixedPoint:
    return FixedPoint(2)
