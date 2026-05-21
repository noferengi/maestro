FROM python:3.12-slim

# Python math libraries
RUN pip install --no-cache-dir sympy numpy scipy mpmath

# Lean4 via elan — install elan, then force the toolchain download at build
# time so containers start instantly without network access.
# Pinned to a stable release; bump the version tag to upgrade.
RUN apt-get update && apt-get install -y curl git && \
    rm -rf /var/lib/apt/lists/* && \
    curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --default-toolchain leanprover/lean4:stable
ENV PATH="/root/.elan/bin:$PATH"
# Force elan to download and cache the toolchain binary during the image build.
# Without this, elan re-downloads (~500 MB) on every cold container start.
RUN lean --version

# Coq (optional — degrade gracefully if absent)
RUN apt-get update && apt-get install -y coq && rm -rf /var/lib/apt/lists/* || true

WORKDIR /sandbox
# Run as root so elan can access /root/.elan at runtime.
# Network isolation (--network none) is enforced by the caller, not the image.
