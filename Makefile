SHELL := /bin/sh
UV := work/tools/uv

.PHONY: bootstrap sync test lint typecheck check doctor run demo docker-up docker-doctor docker-down clean

bootstrap:
	./scripts/bootstrap.sh

sync:
	$(UV) sync --all-groups --frozen

test:
	$(UV) run pytest

lint:
	$(UV) run flake8 src tests

typecheck:
	$(UV) run mypy

check: lint typecheck test

doctor:
	./scripts/doctor.sh

run:
	$(UV) run uvicorn agentic_quant.api:app --host 127.0.0.1 --port 8000 --reload

demo:
	$(UV) run quant-demo

docker-up:
	./scripts/compose.sh up --build -d

docker-doctor:
	./scripts/docker_doctor.sh

docker-down:
	./scripts/compose.sh down

clean:
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache htmlcov
