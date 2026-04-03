.PHONY: install test lint format build docker-build docker-up clean

install:
	pip install -e ".[dev]"

test:
	python -m pytest tests/ -v --ignore=tests/test_training_integration.py

test-integration:
	python -m pytest tests/test_training_integration.py -v

lint:
	python -m py_compile core/pipeline.py
	python -m py_compile core/fast_train_zeroclaw.py
	python -m py_compile core/fast_export.py
	python -m py_compile core/hf_upload.py
	python -m py_compile ui/app.py

format:
	@echo "No formatter configured — add ruff or black to dev deps"

build:
	python -m build

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
