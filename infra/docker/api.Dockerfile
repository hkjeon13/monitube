# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

WORKDIR /workspace

FROM base AS development

COPY apps/api /workspace/apps/api

# A lockfile makes this deterministic; during the initial scaffold uv resolves
# dependencies when the lockfile has not yet been created.
RUN cd /workspace/apps/api \
    && if [ -f uv.lock ]; then uv sync --frozen --all-groups; else uv sync --all-groups; fi

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "--directory", "apps/api", "uvicorn", "monitube_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
