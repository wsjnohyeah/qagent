from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


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
