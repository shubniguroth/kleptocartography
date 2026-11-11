# Klepto Cartographer
# Small single-process image: Python + pytile + aiohttp + apprise. ~70 MB.
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/shubniguroth/kleptocartography" \
      org.opencontainers.image.title="Klepto Cartographer" \
      org.opencontainers.image.description="Self-hosted map of where Tile-tagged equipment was last seen"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KLEPTO_CONFIG=/config/config.toml \
    KLEPTO_DATA_DIR=/data \
    KLEPTO_PORT=8787

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ /app/

# Default non-root user. docker-compose.yml overrides this with PUID:PGID so
# the bind-mounted ./data folder stays owned by your host user.
RUN useradd --system --uid 10001 --no-create-home klepto \
 && mkdir -p /data /config && chown klepto /data
USER klepto

VOLUME ["/data"]
EXPOSE 8787

# Liveness only (is the web server up?). Data freshness is reported separately
# via /healthz?strict=1 and the optional Healthchecks ping.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "klepto"]
