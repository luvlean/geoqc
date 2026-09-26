# syntax=docker/dockerfile:1
# geoqc application image: FastAPI API + Leaflet web map served by uvicorn.
# CPU-only torch keeps the image a few GB smaller than the default CUDA wheels;
# override with --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 if needed.
FROM python:3.11-slim

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    GEOQC_DATA_DIR=/app/data

# Shared libraries required by the opencv / rasterio / pyproj wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash geoqc
WORKDIR /app

# Dependencies first so the heavy layers are cached across code changes.
# torch/torchvision sit in their own layer: a network hiccup later does not redownload them.
RUN pip install --index-url "${TORCH_INDEX_URL}" torch torchvision
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application code. README.md is referenced by pyproject.toml; the glob keeps the
# COPY valid even if the file is absent, and the RUN below provides a stub then.
COPY pyproject.toml README.md* ./
COPY src ./src
COPY config ./config
COPY sql ./sql
COPY web ./web
RUN test -f README.md || echo "# geoqc" > README.md \
    && pip install --no-deps -e .

RUN mkdir -p /app/data /app/reports /app/models && chown -R geoqc:geoqc /app
USER geoqc

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "geoqc.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
