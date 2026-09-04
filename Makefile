SHELL := /bin/sh
UV := work/tools/uv

.PHONY: bootstrap sync migrate test lint typecheck check doctor run demo alpaca-probe docker-up docker-doctor docker-alpaca-probe docker-alpaca-stream docker-down clean

bootstrap:
	./scripts/bootstrap.sh

sync:
	$(UV) sync --all-groups --frozen

migrate:
	$(UV) run alembic upgrade head

test:
	$(UV) run pytest

lint:
	$(UV) run flake8 src tests migrations

typecheck:
	$(UV) run mypy

check: lint typecheck test

doctor:
	./scripts/doctor.sh

run:
	$(UV) run uvicorn agentic_quant.api:app --host 127.0.0.1 --port 8000 --reload

demo:
	$(UV) run quant-demo

alpaca-probe:
	$(UV) run quant-alpaca probe

docker-up:
	./scripts/compose.sh up --build -d

docker-doctor:
	./scripts/docker_doctor.sh

docker-alpaca-probe:
	./scripts/compose.sh exec -T api quant-alpaca probe

docker-alpaca-stream:
	./scripts/compose.sh exec -T api quant-alpaca stream --symbols "$${SYMBOLS:-SPY}" --seconds "$${SECONDS:-60}" --max-frames "$${MAX_FRAMES:-100}"

docker-down:
	./scripts/compose.sh down

clean:
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache htmlcov
