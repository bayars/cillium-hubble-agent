# Network Monitor API with Hubble Integration
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies and Hubble CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Hubble CLI (for flow observation)
ARG HUBBLE_VERSION=v0.13.0
RUN curl -L --remote-name-all https://github.com/cilium/hubble/releases/download/${HUBBLE_VERSION}/hubble-linux-amd64.tar.gz \
    && tar xzvf hubble-linux-amd64.tar.gz \
    && mv hubble /usr/local/bin/ \
    && rm hubble-linux-amd64.tar.gz

# Install uv for package management
COPY --from=docker.io/astral/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml .
COPY api/ ./api/

# Install dependencies with uv
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO
ENV HOST=0.0.0.0
ENV PORT=8000
ENV DEMO_MODE=false
ENV HUBBLE_ENABLED=true
ENV HUBBLE_RELAY_ADDR=hubble-relay.kube-system.svc.cluster.local:4245
ENV IDLE_TIMEOUT_SECONDS=5

# Expose API port
EXPOSE 8000

# Run API server
CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
