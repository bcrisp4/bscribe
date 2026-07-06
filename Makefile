# bscribe developer tasks. All commands run inside the uv-managed venv.

.PHONY: help sync fmt lint typecheck audit test check image

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

sync:  ## Install/refresh the environment from the lockfile
	uv sync --locked

fmt:  ## Format and autofix
	uv run ruff format .
	uv run ruff check --fix .

lint:  ## Lint and format-check
	uv run ruff check --output-format=full .
	uv run ruff format --check .

typecheck:  ## Type-check with pyright (primary) and mypy (secondary)
	uv run pyright
	uv run mypy

audit:  ## Scan dependencies for known vulnerabilities
	uv audit

test:  ## Run the test suite with coverage
	uv run pytest -n auto --cov=bscribe --cov-branch --cov-report=term-missing

check: lint typecheck audit test  ## Run all checks (matches CI)

image:  ## Build a local container image (host arch, no push)
	docker build -t bscribe:dev .
