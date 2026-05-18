FROM python:3.12.13 AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN python -m venv .venv
COPY requirements.txt ./
RUN .venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12.13-slim
WORKDIR /app

COPY --from=builder /app/.venv .venv/
COPY . .

# Run as non-root user (-m creates home dir so gunicorn control server can write)
RUN useradd -r -m -s /bin/false appuser
USER appuser

CMD ["/app/.venv/bin/gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "60", "--worker-tmp-dir", "/dev/shm", "server:app"]
