from __future__ import annotations

import json

from agentic_quant.config import Settings
from agentic_quant.ledger import EventLedger
from agentic_quant.pipeline import run_synthetic_vertical_slice


def main() -> None:
    settings = Settings()
    ledger = EventLedger(settings.database_url)
    ledger.initialize()
    bundle = run_synthetic_vertical_slice(
        settings=settings,
        ledger=ledger,
        new_exposure_paused=settings.global_new_exposure_paused,
    )
    print(json.dumps(bundle.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()

