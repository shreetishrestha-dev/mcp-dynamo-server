# syntax=docker/dockerfile:1.7
# To pin to a specific digest (recommended for production):
#   docker pull python:3.12-slim && docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
# Then replace the FROM lines with: FROM python:3.12-slim@sha256:<digest>

# --- builder ----------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Copy only the metadata first to maximize layer caching for deps.
COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel and install it into a relocatable prefix.
RUN pip install --upgrade pip build && \
    python -m build --wheel --outdir /wheels . && \
    pip install --prefix=/install --no-cache-dir /wheels/*.whl

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user so the container is friendlier in restricted environments.
RUN useradd --create-home --shell /bin/bash mcp

COPY --from=builder /install /usr/local

USER mcp
WORKDIR /home/mcp

# stdio transport: the MCP client pipes JSON-RPC over stdin/stdout.
CMD ["python", "-m", "mcp_dynamo"]
