FROM python:3.11-alpine

RUN pip install --no-cache-dir \
    grpcio \
    grpcio-tools \
    protobuf \
    kubernetes \
    redis

WORKDIR /app

# Copy generated proto stubs (reuse from api/)
COPY api/generated/ /app/api/generated/
COPY api/__init__.py /app/api/__init__.py

# Copy the collector package
COPY hubble_collector/ /app/hubble_collector/

ENV HUBBLE_RELAY_ADDR=hubble-relay.kube-system.svc.cluster.local:4245
ENV HUBBLE_TLS=false
ENV TOPOLOGY_REFRESH_S=30
ENV FLUSH_INTERVAL_S=5
ENV IDLE_TIMEOUT_S=10
ENV LOG_LEVEL=INFO

CMD ["python", "-u", "-m", "hubble_collector.collector"]
