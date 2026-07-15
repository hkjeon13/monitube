# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:${PATH}"

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

WORKDIR /workspace

COPY apps/api /workspace/apps/api
COPY apps/worker /workspace/apps/worker

# The worker currently shares the API package's Python dependencies and keeps
# its own source directory on PYTHONPATH at runtime.
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python /workspace/apps/api

ENV PYTHONPATH=/workspace/apps/api:/workspace/apps/worker

CMD ["python", "-m", "monitube_worker.worker"]
