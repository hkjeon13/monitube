# syntax=docker/dockerfile:1.7
ARG RUST_VERSION=1.85

FROM rust:${RUST_VERSION}-bookworm AS builder
WORKDIR /workspace

COPY Cargo.toml Cargo.lock ./
COPY apps/api-rust ./apps/api-rust
COPY apps/nlp-worker-rust ./apps/nlp-worker-rust
COPY apps/collection-worker-rust ./apps/collection-worker-rust
COPY apps/analysis-worker-rust ./apps/analysis-worker-rust
COPY apps/maintenance-rust ./apps/maintenance-rust
COPY crates ./crates

RUN cargo build --locked --release --package monitube-nlp-worker-rust

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /workspace/target/release/monitube-nlp-worker-rust /usr/local/bin/monitube-nlp-worker-rust

USER nobody
ENTRYPOINT ["/usr/local/bin/monitube-nlp-worker-rust"]
