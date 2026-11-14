# --- build stage: install dependencies only -------------------------------------------------
FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- runtime stage: small image, non-root user ------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/approval.db
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir /data && chown app /data
WORKDIR /srv
COPY --from=build /install /usr/local
COPY approval ./approval
COPY sample_orders ./sample_orders
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status != 200)"
# One worker: SQLite has one writer, and the in-process rate limiter counts per process (see README, "How it scales").
CMD ["uvicorn", "approval.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
