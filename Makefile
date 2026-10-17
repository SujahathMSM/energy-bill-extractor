.PHONY: install test lint run data eval

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .

run:
	uvicorn bill_extractor.api:app --reload

data:
	python -m bill_extractor.synth --n 24 --out data/synthetic

eval:
	python -m bill_extractor.evaluate --data data/synthetic
