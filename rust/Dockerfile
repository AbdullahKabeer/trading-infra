# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM rust:1.82-slim-bookworm AS builder

RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Cache dependencies layer separately
COPY Cargo.toml Cargo.lock* ./
RUN mkdir src && echo "fn main(){}" > src/main.rs && cargo build --release && rm -rf src

# Build actual source
COPY src ./src
RUN touch src/main.rs && cargo build --release

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    ca-certificates \
    libssl3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /build/target/release/es-bot /app/es-bot

# Data volumes
RUN mkdir -p /app/es_sessions /app/es_ticks

EXPOSE 8080

# Docker has no TTY by default — bot runs in headless mode (web dashboard only)
ENV RUST_LOG=info

ENTRYPOINT ["/app/es-bot"]
