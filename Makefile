# Docling Converter — local Docker and serverless static checks.
.PHONY: build build-lambda run test coverage

PYTHON ?= python3

build:
	docker build --tag docling-converter:local .

build-lambda:
	docker build --platform linux/amd64 --file Dockerfile.lambda --tag docling-converter:lambda-local .

run:
	docker run --rm --publish 7860:7860 docling-converter:local

test:
	$(PYTHON) -m unittest discover -s tests -v

coverage:
	$(PYTHON) -m coverage run --source=lambda_app -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=50
