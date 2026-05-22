FROM python:3.12-slim

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
        curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Python math stack ─────────────────────────────────────────────────────────
RUN pip install --no-cache-dir \
    sympy numpy scipy mpmath \
    z3-solver \
    networkx

# ── Lean4 + Lake via elan ─────────────────────────────────────────────────────
# Toolchain version is also pinned in lean-toolchain; keep in sync.
RUN curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
        | sh -s -- -y --no-modify-path
ENV PATH="/root/.elan/bin:$PATH"
# Force toolchain download at build time (avoids 500 MB re-download on cold starts)
COPY lean-toolchain /lean-toolchain
RUN elan toolchain install "$(cat /lean-toolchain)" && \
    elan default "$(cat /lean-toolchain)" && \
    lean --version && lake --version

# ── Mathlib Lake project ──────────────────────────────────────────────────────
# lake exe cache get downloads pre-built .olean files from Mathlib's CI S3
# bucket (~10-15 GB) so build time is minutes not hours.  Fallback: lake build.
WORKDIR /mathlib-project
COPY lakefile.toml lean-toolchain ./
RUN lake update
RUN lake exe cache get || lake build Mathlib

# ── Coq (optional, modern via apt — degrades gracefully if absent) ─────────────
RUN apt-get update && apt-get install -y coq && rm -rf /var/lib/apt/lists/* || true

WORKDIR /sandbox
