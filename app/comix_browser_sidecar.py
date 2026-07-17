#!/usr/bin/env python3
"""Comix browser capture sidecar for Suwayomi staging.

Runs inside the proxy-transparent network namespace so comix.to/static.comix.top
follow the same direct routing as Suwayomi/Flaresolverr.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import signal
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
ZOMBIE_WARN_THRESHOLD = int(os.environ.get("COMIX_ZOMBIE_WARN_THRESHOLD", "10"))
ZOMBIE_EXIT_THRESHOLD = int(os.environ.get("COMIX_ZOMBIE_EXIT_THRESHOLD", "30"))
BROWSER_PROCESS_EXIT_THRESHOLD = int(os.environ.get("COMIX_CHROME_PROCESS_EXIT_THRESHOLD", "80"))
CAPTURE_STUCK_EXIT_SECONDS = int(os.environ.get("COMIX_CAPTURE_STUCK_EXIT_SECONDS", "300"))
WATCHDOG_INTERVAL_SECONDS = float(os.environ.get("COMIX_WATCHDOG_INTERVAL_SECONDS", "10"))
WATCHDOG_SELF_EXIT = os.environ.get("COMIX_WATCHDOG_SELF_EXIT", "true").lower() not in {"0", "false", "no"}
ALLOWED_HOSTS = {"comix.to", "www.comix.to"}
ALLOWED_IMAGE_SUFFIXES = (
    ".wowpic1.store",
    ".wowpic2.store",
    ".wowpic3.store",
    ".wowpic4.store",
    ".comix.to",
    ".comix.top",
)
BROWSER_PROCESS_MARKERS = (
    "chromium",
    "chromedriver",
    "chrome_crashpad",
    "crashpad_handler",
    "chrome_crashpad_handler",
)

capture_lock = threading.Lock()
capture_state_lock = threading.Lock()
capture_state = {
    "in_progress": False,
    "started_at": None,
    "url": None,
    "last_capture_status": None,
    "last_capture_elapsed": None,
    "last_capture_finished_at": None,
    "last_cleanup": None,
}


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


def _read_cmdline(pid: int) -> str:
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _read_processes() -> list[dict]:
    processes: list[dict] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace").read()
            end = stat.rfind(")")
            if end < 0:
                continue
            comm = stat[stat.find("(") + 1:end]
            rest = stat[end + 2 :].split()
            state = rest[0]
            ppid = int(rest[1])
        except (OSError, ValueError, IndexError):
            continue
        args = _read_cmdline(pid) or f"[{comm}]"
        processes.append({"pid": pid, "ppid": ppid, "state": state, "comm": comm, "args": args})
    return processes


def _is_browser_process(process: dict) -> bool:
    haystack = f"{process.get('comm', '')} {process.get('args', '')}".lower()
    if any(marker in haystack for marker in BROWSER_PROCESS_MARKERS):
        return True
    # Chrome child process names are sometimes truncated to "chrome".
    return "chrome" in haystack and "comix_browser_sidecar" not in haystack


def _process_summary(processes: list[dict]) -> dict:
    zombie_counter: Counter[str] = Counter()
    browser_counter: Counter[str] = Counter()
    for process in processes:
        comm = str(process.get("comm") or "?")
        if str(process.get("state") or "").startswith("Z"):
            zombie_counter[comm] += 1
        if _is_browser_process(process):
            browser_counter[comm] += 1
    return {
        "zombies_total": sum(zombie_counter.values()),
        "zombies_by_comm": dict(sorted(zombie_counter.items())),
        "browser_processes_total": sum(browser_counter.values()),
        "browser_processes_by_comm": dict(sorted(browser_counter.items())),
    }


def _relevant_pids(processes: list[dict]) -> set[int]:
    return {int(process["pid"]) for process in processes if _is_browser_process(process)}


def _descendant_pids(processes: list[dict], root_pids: set[int]) -> set[int]:
    children_by_ppid: dict[int, list[int]] = defaultdict(list)
    for process in processes:
        children_by_ppid[int(process["ppid"])].append(int(process["pid"]))
    descendants: set[int] = set()
    stack = list(root_pids)
    while stack:
        parent = stack.pop()
        for child in children_by_ppid.get(parent, []):
            if child in descendants or child in root_pids:
                continue
            descendants.add(child)
            stack.append(child)
    return descendants


def _cleanup_candidate_pids(before_pids: set[int], after_processes: list[dict], root_pids: set[int]) -> set[int]:
    safe_roots = {pid for pid in root_pids if pid and pid > 1}
    after_relevant = _relevant_pids(after_processes)
    candidates = (after_relevant - before_pids) | safe_roots | _descendant_pids(after_processes, safe_roots)
    return {pid for pid in candidates if pid not in {0, 1, os.getpid()}}


def _process_by_pid(processes: list[dict]) -> dict[int, dict]:
    return {int(process["pid"]): process for process in processes}


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_is_zombie(pid: int) -> bool:
    try:
        stat = open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace").read()
        end = stat.rfind(")")
        return stat[end + 2 :].split()[0].startswith("Z")
    except Exception:
        return False


def _reap_children() -> int:
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except OSError:
            break
        if pid == 0:
            break
        reaped += 1
    return reaped


def _wait_for_pids_exit(pids: set[int], timeout: float) -> set[int]:
    deadline = time.time() + timeout
    remaining = set(pids)
    while remaining and time.time() < deadline:
        _reap_children()
        remaining = {pid for pid in remaining if _pid_exists(pid) and not _pid_is_zombie(pid)}
        if remaining:
            time.sleep(0.1)
    return remaining


def _driver_root_pids(driver) -> set[int]:
    roots: set[int] = set()
    browser_pid = getattr(driver, "browser_pid", None)
    if isinstance(browser_pid, int):
        roots.add(browser_pid)
    service = getattr(driver, "service", None)
    service_process = getattr(service, "process", None)
    service_pid = getattr(service_process, "pid", None)
    if isinstance(service_pid, int):
        roots.add(service_pid)
    return roots


def _terminate_pids(pids: set[int], sig: signal.Signals) -> list[int]:
    signaled: list[int] = []
    for pid in sorted(pids, reverse=True):
        if not _pid_exists(pid) or _pid_is_zombie(pid):
            continue
        try:
            os.kill(pid, sig)
            signaled.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            print(f"process-cleanup permission denied pid={pid} signal={sig.name}: {exc}", flush=True)
    return signaled


def _record_cleanup(summary: dict) -> None:
    with capture_state_lock:
        capture_state["last_cleanup"] = summary


def _cleanup_driver(driver, before_pids: set[int], started_at: float) -> None:
    cleanup_started = time.time()
    roots = _driver_root_pids(driver)
    quit_error = None
    try:
        driver.quit()
    except Exception as exc:  # keep original capture result from being masked
        quit_error = repr(exc)

    reaped_initial = _reap_children()
    after_quit = _read_processes()
    candidates = _cleanup_candidate_pids(before_pids, after_quit, roots)
    remaining_after_quit = _wait_for_pids_exit(candidates, 2.0)
    term_pids = _terminate_pids(remaining_after_quit, signal.SIGTERM)
    remaining_after_term = _wait_for_pids_exit(remaining_after_quit, 3.0)
    kill_pids = _terminate_pids(remaining_after_term, signal.SIGKILL)
    remaining_after_kill = _wait_for_pids_exit(remaining_after_term, 1.0)
    reaped_final = _reap_children()
    final_processes = _read_processes()
    final_by_pid = _process_by_pid(final_processes)
    summary = {
        "event": "browser_cleanup",
        "capture_elapsed": round(time.time() - started_at, 3),
        "cleanup_elapsed": round(time.time() - cleanup_started, 3),
        "roots": sorted(roots),
        "candidates": sorted(candidates),
        "sigterm": term_pids,
        "sigkill": kill_pids,
        "remaining": sorted(pid for pid in remaining_after_kill if pid in final_by_pid),
        "reaped": reaped_initial + reaped_final,
        "process_summary": _process_summary(final_processes),
    }
    if quit_error:
        summary["driver_quit_error"] = quit_error
    _record_cleanup(summary)
    print(json.dumps(summary, sort_keys=True), flush=True)


def _capture_status_snapshot() -> dict:
    with capture_state_lock:
        state = dict(capture_state)
    in_progress_seconds = 0.0
    if state.get("in_progress") and state.get("started_at"):
        in_progress_seconds = max(0.0, time.time() - float(state["started_at"]))
    return {
        "in_progress": bool(state.get("in_progress")),
        "in_progress_seconds": round(in_progress_seconds, 3),
        "url": state.get("url"),
        "last_capture_status": state.get("last_capture_status"),
        "last_capture_elapsed": state.get("last_capture_elapsed"),
        "last_capture_finished_at": state.get("last_capture_finished_at"),
        "last_cleanup": state.get("last_cleanup"),
    }


def _mark_capture_started(url: str) -> None:
    with capture_state_lock:
        capture_state.update({"in_progress": True, "started_at": time.time(), "url": url})


def _mark_capture_done(status: str, started_at: float) -> None:
    with capture_state_lock:
        capture_state.update(
            {
                "in_progress": False,
                "started_at": None,
                "url": None,
                "last_capture_status": status,
                "last_capture_elapsed": round(time.time() - started_at, 3),
                "last_capture_finished_at": round(time.time(), 3),
            }
        )


def _build_health_response(
    *,
    process_summary: dict,
    capture_status: dict,
    zombie_warn_threshold: int,
    zombie_exit_threshold: int,
    browser_exit_threshold: int,
    capture_stuck_exit_seconds: int,
) -> tuple[dict, int]:
    reasons: list[str] = []
    status = "ok"
    status_code = 200
    zombies_total = int(process_summary.get("zombies_total") or 0)
    browser_total = int(process_summary.get("browser_processes_total") or 0)
    in_progress_seconds = float(capture_status.get("in_progress_seconds") or 0)

    if zombie_exit_threshold > 0 and zombies_total >= zombie_exit_threshold:
        reasons.append("zombie_exit_threshold")
    if browser_exit_threshold > 0 and browser_total >= browser_exit_threshold:
        reasons.append("browser_process_exit_threshold")
    if (
        capture_stuck_exit_seconds > 0
        and capture_status.get("in_progress")
        and in_progress_seconds >= capture_stuck_exit_seconds
    ):
        reasons.append("capture_stuck_exit_threshold")

    if reasons:
        status = "unhealthy"
        status_code = 503
    elif zombie_warn_threshold > 0 and zombies_total >= zombie_warn_threshold:
        status = "degraded"
        reasons.append("zombie_warn_threshold")

    return (
        {
            "status": status,
            "reasons": reasons,
            "processes": process_summary,
            "capture": capture_status,
            "thresholds": {
                "zombie_warn": zombie_warn_threshold,
                "zombie_exit": zombie_exit_threshold,
                "browser_process_exit": browser_exit_threshold,
                "capture_stuck_exit_seconds": capture_stuck_exit_seconds,
            },
        },
        status_code,
    )


def _health_response() -> tuple[dict, int]:
    return _build_health_response(
        process_summary=_process_summary(_read_processes()),
        capture_status=_capture_status_snapshot(),
        zombie_warn_threshold=ZOMBIE_WARN_THRESHOLD,
        zombie_exit_threshold=ZOMBIE_EXIT_THRESHOLD,
        browser_exit_threshold=BROWSER_PROCESS_EXIT_THRESHOLD,
        capture_stuck_exit_seconds=CAPTURE_STUCK_EXIT_SECONDS,
    )


def _watchdog_loop() -> None:
    while True:
        time.sleep(max(1.0, WATCHDOG_INTERVAL_SECONDS))
        body, status_code = _health_response()
        if status_code >= 500:
            print("watchdog unhealthy: " + json.dumps(body, sort_keys=True), flush=True)
            if WATCHDOG_SELF_EXIT:
                os._exit(70)


def _start_watchdog() -> None:
    thread = threading.Thread(target=_watchdog_loop, name="comix-sidecar-watchdog", daemon=True)
    thread.start()


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
    result_status = "error"
    with capture_lock:
        _mark_capture_started(url)
        driver = None
        before_pids = _relevant_pids(_read_processes())
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
                        result_status = "ok"
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
            result_status = "timeout"
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
                _cleanup_driver(driver, before_pids, started)
            else:
                _reap_children()
            _mark_capture_done(result_status, started)


class Handler(BaseHTTPRequestHandler):
    server_version = "comix-browser-sidecar/0.2"

    def _send(self, code: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/health":
            body, status_code = _health_response()
            self._send(status_code, body)
            return

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
    _start_watchdog()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
