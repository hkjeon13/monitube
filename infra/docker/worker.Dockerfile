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

FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
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

COPY apps/api /workspace/apps/api
COPY apps/worker /workspace/apps/worker

# The worker currently shares the API package's Python dependencies and keeps
# its own source directory on PYTHONPATH at runtime.
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python /workspace/apps/api

RUN python -c "from konlpy.tag import Mecab; assert Mecab().pos('영상 분석') == [('영상', 'NNG'), ('분석', 'NNG')]" \
    && python -c "import nltk; nltk.data.find('tokenizers/punkt_tab'); nltk.data.find('taggers/averaged_perceptron_tagger_eng')"

ENV PYTHONPATH=/workspace/apps/api:/workspace/apps/worker

CMD ["python", "-m", "monitube_worker.worker"]
