FROM python:3.11-slim

# non-root user for security
RUN useradd -m -u 1000 promptolian
WORKDIR /app

# install deps first (layer cache)
COPY requirements-selfhost.txt .
RUN pip install --no-cache-dir -r requirements-selfhost.txt \
 && python -m spacy download en_core_web_sm

# copy only the API code
COPY api/ ./api/

# persistent data volume (SQLite)
RUN mkdir -p /data && chown promptolian:promptolian /data
VOLUME ["/data"]

USER promptolian

ENV PORT=3001 \
    HOST=0.0.0.0 \
    FLASK_DEBUG=0 \
    DB_PATH=/data/promptolian.db

EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3001/health')"

CMD ["python", "api/api.py"]