# Docling Converter — dev & deploy shortcuts.
.PHONY: build run test

build:
	docker build --tag docling-converter:local .

run:
	docker run --rm --publish 7860:7860 docling-converter:local

test:
	python3 -m unittest discover -s tests -v
