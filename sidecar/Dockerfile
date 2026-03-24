FROM python:3.11-alpine

COPY sidecar/common.py /app/common.py
COPY sidecar/agent.py /app/agent.py
WORKDIR /app

CMD ["python", "-u", "agent.py"]
