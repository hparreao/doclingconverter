---
title: Docling Converter
emoji: "📄"
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
full_width: true
disable_embedding: false
suggested_hardware: cpu-basic
---

# Docling Converter

An upload-only document converter built on the official `docling-serve` REST
API. The application serves its responsive HTML, CSS, and JavaScript interface
from the same ASGI process as the conversion API; there is no Streamlit service
and no sidecar container.

## Runtime contract

- `docling-serve` is pinned to `1.32.0` through the official CPU image digest
  `sha256:56f12f30f672272a557beb191be6855ba76478e91bac94e8cdedd52d2c8d9da2`.
- That release includes the validated `docling-slim` `2.124.0` matrix. The
  image adds only the compatible `format-iwork` extra and LibreOffice, required
  for Pages and legacy Office formats respectively.
- The server listens on port `7860`, disables the upstream demonstrator UI,
  and uses `POST /v1/convert/file/async`, `GET /v1/status/poll/{task_id}`,
  `GET /v1/result/{task_id}`, and `GET /health`.
- The browser reads `/openapi.json` at startup and renders only output formats
  and conversion controls advertised by that deployed contract.

The supported input set follows the fixed Docling matrix: PDF, current and
legacy Office, OpenDocument, EPUB, Pages, HTML, Markdown, AsciiDoc, LaTeX, CSV,
images, EML/MSG, WebVTT, BoxNote, Docling JSON, and supported XML variants.
Audio, video, VLM, and EBCDIC are deliberately excluded from this deployment.

## Privacy and limits

Remote-source endpoints are blocked before they reach `docling-serve`; this
deployment accepts local uploads only. The outer ASGI boundary accepts at most
five files, 25 MB each, validates a safe filename plus an extension and useful
magic bytes, and never logs file names or contents. Docling enforces 100 pages
per document and a 10-minute document timeout. One CPU worker processes one job
at a time and polling exposes `queued` or `started` states.

Responses, including results, receive `Cache-Control: no-store`. Results are
single-use and configured for removal within 30 minutes. The interface states
that processing occurs on Hugging Face and that files are temporary. It has no
analytics integration.

## Local validation

Docker is the supported runtime because the official CPU image supplies the
Docling matrix. With Docker available:

```bash
make build
make run
curl --fail http://localhost:7860/health
```

Run the static guardrail checks with `make test`. Exercise the OpenAPI contract
and conversion fixtures only against a built container; do not use personal or
confidential documents as fixtures.

## Hugging Face Space

`README.md` front matter configures a public Docker Space named
`parreao/docling-converter`, CPU Basic, full width, and embedding enabled.
The `Sync Hugging Face Space` workflow creates the Space if necessary and
syncs `main` using only the `HF_TOKEN` GitHub secret. It never requires a token
in source code or in conversation. A Space rebuilds after each pushed commit.
