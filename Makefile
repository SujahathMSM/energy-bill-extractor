.PHONY: help install dev test lint format eval eval-oracle data docker docker-run

help:  ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install:  ## Install the package with dev tools
	pip install -e ".[dev]"

dev:  ## Run the API + UI with auto-reload (mock backend unless .env says otherwise)
	uvicorn bill_extractor.api:app --reload --port $${BILLX_PORT:-8000}

test:  ## Run the test suite (no network, no API key)
	pytest

lint:  ## Lint and check formatting
	ruff check .
	ruff format --check .

format:  ## Fix lint findings and formatting
	ruff check --fix .
	ruff format .

eval:  ## Run the evaluation: Claude if ANTHROPIC_API_KEY is set, else simulated
	python -m evals.run_eval

eval-oracle:  ## Harness self-test: the ground-truth backend must score 100%
	python -m evals.run_eval --backend oracle --no-write

data:  ## Generate the synthetic evaluation set into evals/data/synthetic
	python -m evals.generate_dataset

docker:  ## Build and run API + UI with docker compose (http://localhost:8000)
	docker compose up --build

docker-run:  ## Build the image and run it directly on port 8000 with the mock backend
	docker build -t energy-bill-extractor .
	docker run --rm -p 8000:8000 -e BILLX_BACKEND=mock energy-bill-extractor
