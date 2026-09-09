# Docling Converter — local Docker and serverless static checks.
.PHONY: build build-lambda run test

build:
	docker build --tag docling-converter:local .

build-lambda:
	docker build --platform linux/arm64 --file Dockerfile.lambda --tag docling-converter:lambda-local .

run:
	docker run --rm --publish 7860:7860 docling-converter:local

test:
	python3 -m unittest discover -s tests -v
