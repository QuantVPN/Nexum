from pathlib import Path

import pytest
from typer.testing import CliRunner

from nexum.cli import app
from nexum.db import configure_engine

runner = CliRunner()


@pytest.fixture
def file_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("NEXUM_DATABASE_URL", url)
    from nexum.config import reset_settings_cache

    reset_settings_cache()
    configure_engine(url)
    return url


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and "nexum 0." in result.output


def test_init_db_installs_recipes(file_db: str) -> None:
    result = runner.invoke(app, ["init-db"])
    assert result.exit_code == 0, result.output
    assert "Installed 15 automation recipe(s)" in result.output
    result = runner.invoke(app, ["automations", "list"])
    assert result.exit_code == 0 and "daily at 06:30 UTC" in result.output
    assert runner.invoke(app, ["automations", "install-recipes", "--reset"]).exit_code == 0


def test_seed_then_fire_and_run_due(file_db: str) -> None:
    result = runner.invoke(app, ["seed"])
    assert result.exit_code == 0, result.output
    assert "Demo company loaded" in result.output
    assert runner.invoke(app, ["seed"]).exit_code == 1  # guard against double seeding
    result = runner.invoke(app, ["automations", "fire", "daily-cover"])
    assert result.exit_code == 0 and result.output.startswith("success:")
    result = runner.invoke(app, ["automations", "fire", "nope"])
    assert result.exit_code == 1
    result = runner.invoke(app, ["automations", "run-due"])
    assert result.exit_code == 0 and "Nothing due" in result.output
    result = runner.invoke(app, ["automations", "list"])
    assert "runs=" in result.output and "[on ]" in result.output


def test_create_admin(file_db: str) -> None:
    result = runner.invoke(
        app,
        [
            "create-admin",
            "--email",
            "boss@nexum.local",
            "--name",
            "Boss",
            "--password",
            "supersecret1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Admin boss@nexum.local created" in result.output
    result = runner.invoke(
        app,
        [
            "create-admin",
            "--email",
            "boss@nexum.local",
            "--name",
            "Boss",
            "--password",
            "supersecret1",
        ],
    )
    assert result.exit_code != 0
