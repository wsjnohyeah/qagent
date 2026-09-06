from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from agentic_quant.config import Settings


def _migration_root() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file() and (
            candidate / "migrations" / "env.py"
        ).is_file():
            return candidate
    raise RuntimeError(
        "Unable to locate alembic.ini and migrations; run from the deployment root"
    )


def upgrade_database(database_url: str) -> None:
    project_root = _migration_root()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def require_current_database(database_url: str) -> None:
    """Fail closed when a manually migrated environment is not at the code head."""
    project_root = _migration_root()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    expected = ScriptDirectory.from_config(config).get_current_head()
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    if current != expected:
        raise RuntimeError(
            "Database migration is not current: "
            f"expected {expected}, found {current or 'unversioned'}"
        )


def prepare_database(settings: Settings) -> None:
    """Migrate development automatically; verify-only in production."""
    if settings.auto_migrate:
        upgrade_database(settings.database_url)
    else:
        require_current_database(settings.database_url)
