"""Alembic migrations shipped with the package, plus helpers used by the CLI and init_db."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).parent


def alembic_config(url: str, connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.set_main_option("path_separator", "os")
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as connection:
        command.upgrade(alembic_config(str(engine.url), connection), revision)


def downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as connection:
        command.downgrade(alembic_config(str(engine.url), connection), revision)


def stamp(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as connection:
        command.stamp(alembic_config(str(engine.url), connection), revision)


def current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def pending_changes(engine: Engine) -> list[object]:
    """Differences between the models and the database (empty when in sync)."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    import nexum.models  # noqa: F401
    from nexum.db import Base

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "render_as_batch": True}
        )
        return list(compare_metadata(context, Base.metadata))
