# Pipeline Dockerfile — multi-stage build for ML fine-tuning pipeline
# Supports ROCm and CUDA via build args.
#
# Usage:
#   docker build -t pipeline:latest .
#   docker build --build-arg BASE_IMAGE=rocm/pytorch:latest -t pipeline:rocm .

ARG BASE_IMAGE=python:3.12-slim

# ── Builder stage ──────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS builder

WORKDIR /build

# Install build deps first for layer caching
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir build setuptools wheel

# Copy source
COPY core/ core/
COPY ui/ ui/
COPY data/ data/
COPY activate.sh ./

RUN python -m build --wheel --outdir /build/dist

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS runtime

ARG UID=1000
ARG GID=1000

RUN groupadd -g ${GID} pipeline && \
    useradd -u ${UID} -g ${GID} -m pipeline

WORKDIR /app

# Install the pipeline package
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# Copy non-package files needed at runtime
COPY --chown=pipeline:pipeline ui/index.html /app/ui/
COPY --chown=pipeline:pipeline data/ /app/data/

# Create output volume mount point
RUN mkdir -p /app/output && chown pipeline:pipeline /app/output
VOLUME ["/app/output"]

# ROCm environment variables
ENV HSA_ENABLE_SDMA=0
ENV PYTORCH_HIP_ALLOC_CONF="backend:native,expandable_segments:True"
ENV TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1
ENV HF_HUB_ENABLE_HF_TRANSFER=1
ENV PIPELINE_UI_PORT=7865

USER pipeline

EXPOSE 7865

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:7865/api/state'); r.raise_for_status()" 2>/dev/null || exit 1

CMD ["uvicorn", "ui.app:app", "--host", "0.0.0.0", "--port", "7865"]
