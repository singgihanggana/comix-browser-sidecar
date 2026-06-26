#!/usr/bin/env python3
"""Comix browser capture sidecar for Suwayomi staging.

Runs inside the proxy-transparent network namespace so comix.to/static.comix.top
follow the same direct routing as Suwayomi/Flaresolverr.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import subprocess
import threading
import time
import traceback
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

try:
    import undetected_chromedriver as uc
except Exception:  # pragma: no cover - fallback for images without /app on PYTHONPATH
    uc = None

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

HOST = os.environ.get("COMIX_SIDECAR_HOST", "127.0.0.1")
PORT = int(os.environ.get("COMIX_SIDECAR_PORT", "8193"))
DEFAULT_TIMEOUT = int(os.environ.get("COMIX_CAPTURE_TIMEOUT", "150"))
MAX_TIMEOUT = int(os.environ.get("COMIX_CAPTURE_MAX_TIMEOUT", "240"))
ALLOWED_HOSTS = {"comix.to", "www.comix.to"}
ALLOWED_IMAGE_SUFFIXES = (
    ".wowpic1.store",
    ".wowpic2.store",
    ".wowpic3.store",
    ".wowpic4.store",
    ".comix.to",
    ".comix.top",
)

capture_lock = threading.Lock()


def _js_string(value: str | None) -> str:
    return json.dumps(value or "")


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Only https://comix.to URLs are allowed, got: {url!r}")


def _build_init_script(interface_name: str, initialization_script: str, capture_script: str) -> str:
    return f"""
(() => {{
  const iface = {_js_string(interface_name)};
  window.__hervaComixPayload = null;
  window.__hervaComixLastReset = Date.now();
  window[iface] = {{
    passPayload: function(data) {{
      try {{ window.__hervaComixPayload = String(data); }} catch (e) {{}}
    }},
    resetTimer: function() {{
      try {{ window.__hervaComixLastReset = Date.now(); }} catch (e) {{}}
    }}
  }};
  try {{
    {initialization_script or ''}
  }} catch (e) {{
    console.warn('Comix sidecar initialization script failed', e);
  }}
  const __hervaCaptureSource = {_js_string(capture_script)};
  const __hervaRunCapture = () => {{
    try {{
      const result = (0, eval)(__hervaCaptureSource);
      if (typeof result === 'string' && result.length > 0 && !window.__hervaComixPayload) {{
        window.__hervaComixPayload = result;
      }}
    }} catch (e) {{
      try {{ window.__hervaComixLastError = String(e && (e.stack || e.message) || e); }} catch (_) {{}}
    }}
  }};
  __hervaRunCapture();
  window.__hervaComixCaptureInterval = setInterval(__hervaRunCapture, 250);
}})();
"""


def _chromium_major() -> int | None:
    try:
        out = subprocess.check_output([os.environ.get("CHROME_BIN", "/usr/bin/chromium"), "--version"], text=True, timeout=5)
        match = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _new_driver():
    if uc is not None:
        options = uc.ChromeOptions()
        options.binary_location = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1365,2048")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
        major = int(os.environ.get("CHROME_VERSION_MAIN") or (_chromium_major() or 0))
        if major > 0:
            return uc.Chrome(options=options, use_subprocess=True, version_main=major)
        return uc.Chrome(options=options, use_subprocess=True)

    options = Options()
    options.binary_location = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1365,2048")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
    chromedriver = os.environ.get("CHROMEDRIVER_BIN")
    if chromedriver and os.path.exists(chromedriver):
        return webdriver.Chrome(service=Service(chromedriver), options=options)
    return webdriver.Chrome(options=options)


def capture_payload(url: str, initialization_script: str = "", capture_script: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    _validate_url(url)
    if not capture_script.strip():
        raise ValueError("Missing capture script")
    timeout = max(5, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    interface_name = "HervaComixCapture"
    init = _build_init_script(interface_name, initialization_script, capture_script)

    started = time.time()
    with capture_lock:
        driver = None
        try:
            driver = _new_driver()
            try:
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": init})
            except Exception:
                pass
            driver.set_page_load_timeout(min(timeout, 90))
            driver.get(url)
            try:
                driver.execute_script(init)
            except Exception:
                pass

            deadline = time.time() + timeout
            last_payload = None
            while time.time() < deadline:
                try:
                    payload = driver.execute_script("return window.__hervaComixPayload || null")
                    if isinstance(payload, str) and payload:
                        last_payload = payload
                        parsed = json.loads(payload)
                        return {
                            "status": "ok",
                            "url": url,
                            "elapsed": round(time.time() - started, 3),
                            "payload": payload,
                            "payload_type": type(parsed).__name__,
                        }
                except Exception:
                    pass
                time.sleep(0.25)

            title = ""
            current = ""
            try:
                title = driver.title
                current = driver.current_url
            except Exception:
                pass
            return {
                "status": "timeout",
                "url": url,
                "current_url": current,
                "title": title,
                "elapsed": round(time.time() - started, 3),
                "last_payload_prefix": (last_payload or "")[:200],
            }
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "comix-browser-sidecar/0.1"

    def _send(self, code: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return

        parsed_path = urlparse(self.path)
        if parsed_path.path == "/image":
            try:
                target = parse_qs(parsed_path.query).get("url", [""])[0]
                parsed_target = urlparse(target)
                host = (parsed_target.hostname or "").lower()
                if parsed_target.scheme != "https" or not any(host == s.lstrip('.') or host.endswith(s) for s in ALLOWED_IMAGE_SUFFIXES):
                    raise ValueError(f"Image host not allowed: {host}")
                upstream = Request(
                    target,
                    headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                        "Referer": "https://comix.to/",
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    },
                )
                with urlopen(upstream, timeout=60) as response:
                    body = response.read()
                    self.send_response(response.status)
                    for header in (
                        "Content-Type",
                        "Cache-Control",
                        "Last-Modified",
                        "ETag",
                        "X-Scramble-Seed",
                        "X-Scramble-Grid",
                        "X-Scramble-Algo",
                        "X-Scramble-Hash",
                        "X-Enc-Seed",
                        "X-Enc-Algo",
                        "X-Enc-Len",
                    ):
                        value = response.headers.get(header)
                        if value:
                            self.send_header(header, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                return
            except Exception as e:
                self._send(502, {"status": "error", "error": str(e)})
                return

        self._send(404, {"status": "error", "error": "not found"})

    def do_POST(self):
        if self.path != "/capture":
            self._send(404, {"status": "error", "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            result = capture_payload(
                url=data.get("url", ""),
                initialization_script=data.get("initializationScript", ""),
                capture_script=data.get("script", ""),
                timeout=int(data.get("timeout", DEFAULT_TIMEOUT)),
            )
            self._send(200 if result.get("status") == "ok" else 504, result)
        except Exception as e:
            self._send(500, {"status": "error", "error": str(e), "trace": traceback.format_exc()[-3000:]})

    def log_message(self, fmt, *args):
        print(f"{self.log_date_time_string()} - {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"Starting Comix browser sidecar on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
