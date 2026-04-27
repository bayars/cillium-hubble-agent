FROM python:3.11-alpine

RUN pip install --no-cache-dir redis kubernetes

WORKDIR /app

COPY sidecar/ /app/sidecar/

ENV API_PORT=9000
ENV LOG_LEVEL=INFO

EXPOSE 9000

CMD ["python", "-u", "-m", "sidecar.agent"]
