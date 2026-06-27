FROM ghcr.io/flaresolverr/flaresolverr:latest

LABEL org.opencontainers.image.title="comix-browser-sidecar" \
      org.opencontainers.image.description="Headless Chromium sidecar for the patched Suwayomi Comix extension" \
      org.opencontainers.image.source="https://github.com/singgihanggana/comix-browser-sidecar" \
      org.opencontainers.image.licenses="MIT"

ENV COMIX_SIDECAR_HOST=0.0.0.0 \
    COMIX_SIDECAR_PORT=8193 \
    COMIX_CAPTURE_TIMEOUT=150 \
    COMIX_CAPTURE_MAX_TIMEOUT=240 \
    PYTHONUNBUFFERED=1

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY app/comix_browser_sidecar.py /app/comix_browser_sidecar.py

EXPOSE 8193

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('COMIX_SIDECAR_PORT','8193'), timeout=5).read()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/comix_browser_sidecar.py"]
