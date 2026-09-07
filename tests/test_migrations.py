"""Alembic migrations stay in sync with the models and the CLI drives them."""

from pathlib import Path

from sqlalchemy import create_engine, inspect
from typer.testing import CliRunner

from nexum import migrations
from nexum.cli import app
from nexum.db import Base, get_engine, init_db

runner = CliRunner()


def test_upgrade_from_empty_matches_models(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
    assert migrations.current_revision(engine) is None
    migrations.upgrade(engine)
    assert migrations.current_revision(engine) == migrations.head_revision()
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)
    assert migrations.pending_changes(engine) == []
    migrations.downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}


def test_init_db_stamps_head() -> None:
    init_db()
    assert migrations.current_revision(get_engine()) == migrations.head_revision()
    assert migrations.pending_changes(get_engine()) == []


def test_db_cli(tmp_path: Path, monkeypatch) -> None:
    from nexum.config import reset_settings_cache
    from nexum.db import configure_engine

    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("NEXUM_DATABASE_URL", url)
    reset_settings_cache()
    configure_engine(url)
    result = runner.invoke(app, ["db", "current"])
    assert result.exit_code == 0 and "current=none" in result.output and "behind" in result.output
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0 and "(was empty)" in result.output
    result = runner.invoke(app, ["db", "check"])
    assert result.exit_code == 0 and "in sync" in result.output
    result = runner.invoke(app, ["db", "current"])
    assert "up to date" in result.output
    result = runner.invoke(app, ["db", "downgrade", "base"], input="y\n")
    assert result.exit_code == 0 and "at base" in result.output
