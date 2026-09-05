from __future__ import annotations

from pathlib import Path

import pytest

from agentic_quant.config import Settings, TradingMode


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = Path(__file__).parents[1]
    return Settings(
        _env_file=None,
        trading_mode=TradingMode.SHADOW,
        live_trading_enabled=False,
        global_new_exposure_paused=False,
        auth_required=False,
        shadow_runtime_enabled=False,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
        risk_policy_path=root / "configs/risk_policy.yaml",
        restricted_securities_path=root / "configs/restricted_securities.yaml",
        llm_openai_api_key=None,
        llm_meta_api_key=None,
    )
