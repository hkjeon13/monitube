# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS mecab-builder

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       build-essential ca-certificates file libtool make pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY infra/mecab /opt/monitube-mecab
RUN chmod +x /opt/monitube-mecab/install.sh \
    && /opt/monitube-mecab/install.sh

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    LD_LIBRARY_PATH="/usr/local/lib" \
    NLTK_DATA="/usr/local/share/nltk_data"

RUN apt-get update \
    && apt-get install --no-install-recommends -y default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

COPY infra/nltk /opt/monitube-nltk
RUN chmod +x /opt/monitube-nltk/install.sh \
    && /opt/monitube-nltk/install.sh

COPY --from=mecab-builder /usr/local/bin/mecab /usr/local/bin/mecab
COPY --from=mecab-builder /usr/local/lib/libmecab.so.2.0.0 /usr/local/lib/libmecab.so.2.0.0
COPY --from=mecab-builder /usr/local/lib/mecab /usr/local/lib/mecab
COPY --from=mecab-builder /usr/local/etc/mecabrc /usr/local/etc/mecabrc
RUN ln -s libmecab.so.2.0.0 /usr/local/lib/libmecab.so.2 \
    && ln -s libmecab.so.2.0.0 /usr/local/lib/libmecab.so \
    && ldconfig

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

WORKDIR /workspace
COPY apps/tokenizer /workspace/apps/tokenizer
RUN uv sync --directory /workspace/apps/tokenizer --frozen --no-dev \
    && python -c "from monitube_tokenizer.analyzer import MecabNltkNounAnalyzer, analyzer_health; analyzer_health(MecabNltkNounAnalyzer())"

USER nobody
EXPOSE 8010

CMD ["uvicorn", "monitube_tokenizer.main:app", "--app-dir", "/workspace/apps/tokenizer", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
