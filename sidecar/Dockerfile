FROM python:3.11-alpine

RUN pip install --no-cache-dir redis

COPY sidecar/common.py /app/common.py
COPY sidecar/agent.py /app/agent.py
WORKDIR /app

EXPOSE 9000

CMD ["python", "-u", "agent.py"]
