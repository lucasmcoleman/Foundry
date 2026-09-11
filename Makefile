.PHONY: install test test-integration lint format build docker-build docker-up docker-down clean

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest tests/ -v --ignore=tests/test_training_integration.py -m "not slow and not gpu"
	$(PYTHON) -m pytest foundry_gym/tests/ -q

test-integration:
	$(PYTHON) -m pytest tests/test_training_integration.py -v

lint:
	$(PYTHON) -m compileall -q core/ ui/
	$(PYTHON) -m ruff check --select F core/ ui/ tests/ tools/check_wheel.py

format:
	$(PYTHON) -m ruff format core/ ui/ tests/

build:
	$(PYTHON) -m build

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf build/ dist/ *.egg-info/ __pycache__/ .pytest_cache/
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
