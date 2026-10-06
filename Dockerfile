# python:3.11-slim matches the local interpreter, so what is tested is what ships.
# No ffmpeg, no model weights, no build toolchain: the image is ~120 MB and the
# container's resident set is ~80 MB, which is what lets this run on a 512 MB
# free instance.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so edits to application code reuse the cached layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run unprivileged.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Render (and most platforms) inject $PORT; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

# One worker deliberately. There is no model to duplicate any more, but 0.1 CPU
# cannot usefully serve two, and concurrency is already bounded in-process by a
# semaphore sized to the provider's rate limit.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-server-header"]
