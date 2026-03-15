FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY openclaw_memory/ openclaw_memory/
COPY examples/ examples/

RUN useradd -m ocworker
USER ocworker

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "openclaw_memory.cli"]
