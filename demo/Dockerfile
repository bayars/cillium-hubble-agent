# Network Monitor API with Hubble Integration
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (gcc for grpcio compilation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install uv for package management
COPY --from=docker.io/astral/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml uv.lock* ./
COPY api/ ./api/

# Install dependencies with uv
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO
ENV HOST=0.0.0.0
ENV PORT=8000
ENV HUBBLE_ENABLED=true
ENV HUBBLE_RELAY_ADDR=hubble-relay.kube-system.svc.cluster.local:4245
ENV HUBBLE_TLS=false
ENV IDLE_TIMEOUT_SECONDS=5

# Expose API port
EXPOSE 8000

# Run API server
CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
