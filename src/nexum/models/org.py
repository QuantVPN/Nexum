"""Organisation structure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexum.db import Base

if TYPE_CHECKING:
    from nexum.models.people import Employee
    from nexum.models.scheduling import ScheduleTemplate, Shift


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "employees.id", ondelete="SET NULL", use_alter=True, name="fk_department_manager"
        ),
        nullable=True,
    )

    employees: Mapped[list[Employee]] = relationship(
        back_populates="department", foreign_keys="Employee.department_id"
    )
    manager: Mapped[Employee | None] = relationship(foreign_keys=[manager_id], post_update=True)
    shifts: Mapped[list[Shift]] = relationship(back_populates="department")
    templates: Mapped[list[ScheduleTemplate]] = relationship(
        back_populates="department", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Department {self.name}>"
