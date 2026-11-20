FROM python:3.12-slim

LABEL org.opencontainers.image.title="Bookhoard" \
      org.opencontainers.image.description="Self-hosted e-book library with OPDS for KOReader, lubimyczytac.pl metadata and Anna's Archive downloads"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data LIBRARY_DIR=/books INGEST_DIR=/ingest \
    PUID=1000 PGID=1000 UMASK=002

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh && mkdir -p /data /books /ingest

EXPOSE 8080
VOLUME ["/data", "/books", "/ingest"]
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

# entrypoint fixes folder ownership, then drops root and runs as PUID:PGID
ENTRYPOINT ["/app/entrypoint.sh"]
# ONE worker on purpose: the ingest watcher and download queue run as threads inside it
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "180", "--bind", "0.0.0.0:8080", "wsgi:app"]
