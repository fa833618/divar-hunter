# Divar deal hunter — small, non-root runtime image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code changes don't invalidate the layer cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py persian.py divar_client.py filters.py storage.py notifier.py main.py ./
COPY tests ./tests

# State and logs live on volumes; the process itself runs unprivileged.
RUN useradd --create-home --uid 10001 hunter \
    && mkdir -p /app/data /app/logs \
    && chown -R hunter:hunter /app
USER hunter

VOLUME ["/app/data", "/app/logs"]

# Cheap liveness probe: is the python process still there?
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python main.py" > /dev/null || exit 1

CMD ["python", "main.py"]
