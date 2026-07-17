import importlib.util
import sys
import types
from pathlib import Path


def load_sidecar_module():
    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    setattr(webdriver, "Chrome", object)
    chrome = types.ModuleType("selenium.webdriver.chrome")
    options = types.ModuleType("selenium.webdriver.chrome.options")
    service = types.ModuleType("selenium.webdriver.chrome.service")

    class DummyOptions:
        def add_argument(self, *_args, **_kwargs):
            pass

    class DummyService:
        def __init__(self, *_args, **_kwargs):
            pass

    setattr(options, "Options", DummyOptions)
    setattr(service, "Service", DummyService)
    sys.modules.update(
        {
            "selenium": selenium,
            "selenium.webdriver": webdriver,
            "selenium.webdriver.chrome": chrome,
            "selenium.webdriver.chrome.options": options,
            "selenium.webdriver.chrome.service": service,
        }
    )

    module_path = Path(__file__).resolve().parents[1] / "app" / "comix_browser_sidecar.py"
    spec = importlib.util.spec_from_file_location("sidecar_under_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_process_summary_counts_zombies_and_browser_processes():
    sidecar = load_sidecar_module()

    summary = sidecar._process_summary(
        [
            {"pid": 1, "ppid": 0, "state": "S", "comm": "python", "args": "python3 /app/comix_browser_sidecar.py"},
            {"pid": 11, "ppid": 1, "state": "Z", "comm": "chromium", "args": "[chromium] <defunct>"},
            {"pid": 12, "ppid": 1, "state": "Z", "comm": "chrome_crashpad", "args": "[chrome_crashpad] <defunct>"},
            {"pid": 13, "ppid": 1, "state": "S", "comm": "chromedriver", "args": "chromedriver --port=123"},
            {"pid": 14, "ppid": 1, "state": "S", "comm": "bash", "args": "bash"},
        ]
    )

    assert summary["zombies_total"] == 2
    assert summary["zombies_by_comm"] == {"chromium": 1, "chrome_crashpad": 1}
    assert summary["browser_processes_total"] == 3
    assert summary["browser_processes_by_comm"] == {"chromium": 1, "chrome_crashpad": 1, "chromedriver": 1}


def test_health_response_reports_unhealthy_when_exit_threshold_crossed():
    sidecar = load_sidecar_module()

    body, status_code = sidecar._build_health_response(
        process_summary={
            "zombies_total": 31,
            "zombies_by_comm": {"chromium": 31},
            "browser_processes_total": 31,
            "browser_processes_by_comm": {"chromium": 31},
        },
        capture_status={"in_progress": False, "in_progress_seconds": 0, "last_capture_status": "ok"},
        zombie_warn_threshold=10,
        zombie_exit_threshold=30,
        browser_exit_threshold=80,
        capture_stuck_exit_seconds=300,
    )

    assert status_code == 503
    assert body["status"] == "unhealthy"
    assert "zombie_exit_threshold" in body["reasons"]


def test_cleanup_candidates_include_new_browser_descendants_only():
    sidecar = load_sidecar_module()

    before_pids = {100}
    after_processes = [
        {"pid": 100, "ppid": 1, "state": "S", "comm": "chromium", "args": "chromium old"},
        {"pid": 200, "ppid": 1, "state": "S", "comm": "chromium", "args": "chromium new"},
        {"pid": 201, "ppid": 200, "state": "S", "comm": "chrome_crashpad", "args": "chrome_crashpad_handler"},
        {"pid": 300, "ppid": 1, "state": "S", "comm": "bash", "args": "bash"},
    ]

    candidates = sidecar._cleanup_candidate_pids(before_pids, after_processes, root_pids={200})

    assert candidates == {200, 201}
