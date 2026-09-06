"""Clock-in / clock-out records."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexum.db import Base
from nexum.models.types import StrEnum, UTCDateTime, str_enum, utcnow

if TYPE_CHECKING:
    from nexum.models.people import Employee
    from nexum.models.scheduling import Shift


class TimeEntryStatus(StrEnum):
    OPEN = "open"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("shifts.id", ondelete="SET NULL"), nullable=True
    )
    clock_in: Mapped[datetime] = mapped_column(UTCDateTime)
    clock_out: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[TimeEntryStatus] = mapped_column(
        str_enum(TimeEntryStatus, "time_entry_status"), default=TimeEntryStatus.OPEN
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    employee: Mapped[Employee] = relationship(back_populates="time_entries")
    shift: Mapped[Shift | None] = relationship(back_populates="time_entries")

    @property
    def worked_hours(self) -> float:
        if self.clock_out is None:
            return 0.0
        seconds = (self.clock_out - self.clock_in).total_seconds() - self.break_minutes * 60
        return max(seconds, 0) / 3600.0
