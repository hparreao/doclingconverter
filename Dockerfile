# syntax=docker/dockerfile:1
# docling-serve v1.32.0 CPU image, pinned to the manifest verified on 2026-09-08.
FROM quay.io/docling-project/docling-serve-cpu:v1.32.0@sha256:56f12f30f672272a557beb191be6855ba76478e91bac94e8cdedd52d2c8d9da2

USER root

# LibreOffice is required by Docling for legacy Office files. format-iwork is
# installed against the Docling version already validated by docling-serve.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'docling-slim[format-iwork]==2.124.0'

WORKDIR /app
COPY app /app/app

ENV UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=7860 \
    UVICORN_WORKERS=1 \
    DOCLING_DEVICE=cpu \
    DOCLING_SERVE_ENABLE_UI=false \
    DOCLING_SERVE_LOG_LEVEL=WARNING \
    DOCLING_SERVE_DEBUG_ERROR_DETAILS=false \
    DOCLING_SERVE_ENABLE_REMOTE_SERVICES=false \
    DOCLING_SERVE_ALLOW_EXTERNAL_PLUGINS=false \
    DOCLING_SERVE_MAX_FILE_SIZE=26214400 \
    DOCLING_SERVE_MAX_NUM_PAGES=100 \
    DOCLING_SERVE_MAX_DOCUMENT_TIMEOUT=600 \
    DOCLING_SERVE_MAX_SOURCES_PER_REQUEST=5 \
    DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1 \
    DOCLING_SERVE_ENG_LOC_SHARE_MODELS=true \
    DOCLING_SERVE_SINGLE_USE_RESULTS=true \
    DOCLING_SERVE_RESULT_REMOVAL_DELAY=1800

EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
