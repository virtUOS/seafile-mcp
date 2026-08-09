FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits don't invalidate the layer.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 seafile

USER seafile

# Not published to the host: Caddy reaches this over the internal compose network.
EXPOSE 8000

# No HEALTHCHECK hitting /mcp — it is credential-guarded and would log a 401 per probe.
CMD ["python", "-m", "seafile_mcp", "--transport", "http"]
