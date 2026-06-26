# Comix Browser Sidecar

Headless Chromium sidecar for Singgih's patched Suwayomi Comix extension.

The patched Comix extension calls this service instead of trying to start Android `WebView` inside Suwayomi Server.

## Image

```text
ghcr.io/singgihanggana/comix-browser-sidecar:latest
```

## Runtime

Default environment:

```text
COMIX_SIDECAR_HOST=0.0.0.0
COMIX_SIDECAR_PORT=8193
COMIX_CAPTURE_TIMEOUT=150
COMIX_CAPTURE_MAX_TIMEOUT=240
```

Endpoints:

- `GET /health`
- `POST /capture`
- `GET /image?url=https://...`

## Suwayomi compose shape

```yaml
comix-browser-sidecar:
  image: ghcr.io/singgihanggana/comix-browser-sidecar:latest
  container_name: comix-browser-sidecar
  restart: unless-stopped
  shm_size: "1gb"
  environment:
    - COMIX_SIDECAR_HOST=0.0.0.0
    - COMIX_SIDECAR_PORT=8193
    - COMIX_CAPTURE_TIMEOUT=150
    - COMIX_CAPTURE_MAX_TIMEOUT=240
  healthcheck:
    test: ["CMD-SHELL", "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8193/health', timeout=5).read()\" || exit 1"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 30s
```
