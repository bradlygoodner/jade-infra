# Service Health Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python health monitor that checks all Docker services every 30s, auto-restarts on failure, and sends notifications through n8n → Pushover → Uptime Kuma tiers.

**Architecture:** A single long-running Python script (`healthmonitor.py`) managed by systemd. Uses only Python 3 standard library (`urllib.request`, `subprocess`, `json`, `logging`, `dataclasses`). Notifications flow through n8n webhook (primary), direct Pushover API (fallback), and Uptime Kuma heartbeat (dead man's switch). An n8n workflow handles logging to Baserow and LLM-assisted diagnosis for emergencies.

**Tech Stack:** Python 3.10 (stdlib only), systemd, Docker Compose CLI, n8n webhooks, Pushover API, Uptime Kuma push monitors

**Spec:** `docs/superpowers/specs/2026-04-07-service-health-monitor-design.md`

---

## File Structure

```
docker/scripts/
  healthmonitor.py              # Main monitor script (new)
  config.env                    # Extended with new variables (modify)
  test_healthmonitor.py         # Tests (new)

/etc/systemd/system/
  propertyops-healthmonitor.service  # Systemd unit (new)
```

---

### Task 1: NotificationManager — Send Alerts via Three Tiers

The notification layer is foundational — every other component depends on it. Build and test it first.

**Files:**
- Create: `docker/scripts/healthmonitor.py` (initial file with NotificationManager)
- Create: `docker/scripts/test_healthmonitor.py`
- Modify: `docker/scripts/config.env` (add new variables)

- [ ] **Step 1: Add notification config vars to config.env**

Append to `docker/scripts/config.env`:

```bash
# ── Health Monitor ───────────────────────────────────────────────────────────
CHECK_INTERVAL=30            # seconds between check cycles
FAILURE_THRESHOLD=2          # consecutive failures before restart
COOLDOWN_PERIOD=300          # seconds between restarts per service
MAX_RESTARTS=3               # max restarts per 30-minute window
REQUEST_TIMEOUT=10           # seconds per HTTP health check
RESTART_WINDOW=1800          # rolling window for max restart tracking (30 min)

# ── Pushover (direct fallback) ──────────────────────────────────────────────
PUSHOVER_APP_TOKEN=""
PUSHOVER_USER_KEY=""

# ── Uptime Kuma ─────────────────────────────────────────────────────────────
UPTIME_KUMA_PUSH_URL=""
```

- [ ] **Step 2: Commit config.env changes**

```bash
git add docker/scripts/config.env
git commit -m "feat: add health monitor config variables to config.env"
```

- [ ] **Step 3: Write failing test for NotificationManager**

Create `docker/scripts/test_healthmonitor.py`:

```python
"""Tests for healthmonitor.py — run with: python3 -m pytest docker/scripts/test_healthmonitor.py -v"""
import json
import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Add scripts dir to path
sys.path.insert(0, os.path.dirname(__file__))

from healthmonitor import NotificationManager, load_config


class TestNotificationManager(unittest.TestCase):
    def setUp(self):
        self.config = {
            "n8n_webhook_base": "http://localhost:15678/webhook",
            "pushover_app_token": "test_app_token",
            "pushover_user_key": "test_user_key",
            "uptime_kuma_push_url": "http://localhost:19999/api/push/test",
            "request_timeout": 5,
        }
        self.nm = NotificationManager(self.config)

    def test_build_pushover_payload_normal(self):
        payload = self.nm._build_pushover_payload(
            service="baserow",
            event_type="restart_initiated",
            message="Baserow restarting",
        )
        self.assertEqual(payload["token"], "test_app_token")
        self.assertEqual(payload["user"], "test_user_key")
        self.assertEqual(payload["priority"], "0")
        self.assertIn("baserow", payload["title"].lower())

    def test_build_pushover_payload_emergency(self):
        payload = self.nm._build_pushover_payload(
            service="baserow",
            event_type="emergency",
            message="Baserow failed 3 restarts",
        )
        self.assertEqual(payload["priority"], "2")
        self.assertEqual(payload["retry"], "60")
        self.assertEqual(payload["expire"], "3600")

    def test_build_pushover_payload_recovery(self):
        payload = self.nm._build_pushover_payload(
            service="n8n",
            event_type="recovery",
            message="n8n recovered",
        )
        self.assertEqual(payload["priority"], "-1")

    def test_build_webhook_payload(self):
        payload = self.nm._build_webhook_payload(
            service="baserow",
            status="unhealthy",
            event_type="restart_initiated",
            message="restarting",
            restart_count=1,
            check_type="internal",
        )
        self.assertEqual(payload["service"], "baserow")
        self.assertEqual(payload["event_type"], "restart_initiated")
        self.assertIn("timestamp", payload)

    @patch("healthmonitor.urlopen")
    def test_send_n8n_webhook_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"ok"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = self.nm._send_n8n_webhook(
            service="baserow",
            status="unhealthy",
            event_type="restart_initiated",
            message="restarting",
            restart_count=1,
            check_type="internal",
        )
        self.assertTrue(result)

    @patch("healthmonitor.urlopen")
    def test_send_n8n_webhook_failure_falls_through(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")

        result = self.nm._send_n8n_webhook(
            service="baserow",
            status="unhealthy",
            event_type="restart_initiated",
            message="restarting",
            restart_count=1,
            check_type="internal",
        )
        self.assertFalse(result)

    @patch("healthmonitor.urlopen")
    def test_notify_skips_n8n_when_n8n_is_down_service(self, mock_urlopen):
        """When n8n is the failing service, go straight to Pushover."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"ok"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        self.nm.notify(
            service="n8n",
            status="unhealthy",
            event_type="restart_initiated",
            message="n8n restarting",
            restart_count=1,
            check_type="internal",
        )
        # Should have called urlopen for Pushover directly, not n8n webhook
        call_args = mock_urlopen.call_args_list
        urls = [str(call) for call in call_args]
        # Should NOT contain the n8n webhook URL
        for call_str in urls:
            self.assertNotIn("/webhook/health-alert", call_str)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py -v`
Expected: ImportError — `healthmonitor` module doesn't exist yet.

- [ ] **Step 5: Write NotificationManager implementation**

Create `docker/scripts/healthmonitor.py` with the initial NotificationManager:

```python
#!/usr/bin/env python3
"""
PropertyOps Service Health Monitor

A long-running systemd service that monitors Docker services,
auto-restarts on failure, and sends tiered notifications.

Usage: python3 healthmonitor.py
Config: Reads from config.env in the same directory.
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH = Path("/root/docker/logs/healthmonitor.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("healthmonitor")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.FileHandler(LOG_PATH)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config from config.env, return dict with typed values."""
    config_path = Path(__file__).parent / "config.env"
    raw = {}
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            # Strip inline comments and quotes
            value = value.split("#")[0].strip().strip('"').strip("'")
            raw[key.strip()] = value

    return {
        "n8n_webhook_base": raw.get("N8N_WEBHOOK_BASE", "http://localhost:5678/webhook"),
        "pushover_app_token": raw.get("PUSHOVER_APP_TOKEN", ""),
        "pushover_user_key": raw.get("PUSHOVER_USER_KEY", ""),
        "uptime_kuma_push_url": raw.get("UPTIME_KUMA_PUSH_URL", ""),
        "check_interval": int(raw.get("CHECK_INTERVAL", "30")),
        "failure_threshold": int(raw.get("FAILURE_THRESHOLD", "2")),
        "cooldown_period": int(raw.get("COOLDOWN_PERIOD", "300")),
        "max_restarts": int(raw.get("MAX_RESTARTS", "3")),
        "request_timeout": int(raw.get("REQUEST_TIMEOUT", "10")),
        "restart_window": int(raw.get("RESTART_WINDOW", "1800")),
        "compose_base": raw.get("COMPOSE_BASE", "/root/docker"),
    }


# ── Notification Manager ────────────────────────────────────────────────────

PUSHOVER_PRIORITY_MAP = {
    "failure_detected": "0",
    "restart_initiated": "0",
    "restart_success": "0",
    "restart_failed": "0",
    "tunnel_issue": "0",
    "emergency": "2",
    "recovery": "-1",
}


class NotificationManager:
    """Three-tier notification: n8n webhook -> direct Pushover -> Uptime Kuma heartbeat."""

    def __init__(self, config: dict):
        self.config = config

    def notify(self, *, service: str, status: str, event_type: str,
               message: str, restart_count: int = 0, check_type: str = "internal"):
        """Send notification through available tiers."""
        # Tier 1: n8n webhook (skip if n8n is the failing service)
        n8n_ok = False
        if service != "n8n":
            n8n_ok = self._send_n8n_webhook(
                service=service, status=status, event_type=event_type,
                message=message, restart_count=restart_count, check_type=check_type,
            )

        # Tier 2: Direct Pushover (if n8n failed or n8n is down)
        if not n8n_ok:
            self._send_pushover(service=service, event_type=event_type, message=message)

        # Tier 3: Uptime Kuma heartbeat is sent every cycle in the main loop,
        # not per-notification. It's a dead man's switch, not an alert channel.

    def send_heartbeat(self):
        """Send Uptime Kuma push heartbeat. Called every check cycle."""
        url = self.config.get("uptime_kuma_push_url", "")
        if not url:
            return
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=5) as resp:
                resp.read()
            logger.debug("Uptime Kuma heartbeat sent")
        except (URLError, HTTPError, OSError) as e:
            logger.warning("Failed to send Uptime Kuma heartbeat: %s", e)

    def _build_webhook_payload(self, *, service: str, status: str, event_type: str,
                               message: str, restart_count: int, check_type: str) -> dict:
        return {
            "service": service,
            "status": status,
            "event_type": event_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "restart_count": restart_count,
            "check_type": check_type,
        }

    def _send_n8n_webhook(self, *, service: str, status: str, event_type: str,
                          message: str, restart_count: int, check_type: str) -> bool:
        """Send to n8n webhook. Returns True on success."""
        url = f"{self.config['n8n_webhook_base']}/health-alert"
        payload = self._build_webhook_payload(
            service=service, status=status, event_type=event_type,
            message=message, restart_count=restart_count, check_type=check_type,
        )
        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(req, timeout=self.config["request_timeout"]) as resp:
                resp.read()
            logger.info("n8n webhook sent: %s %s", service, event_type)
            return True
        except (URLError, HTTPError, OSError) as e:
            logger.warning("n8n webhook failed (%s), falling back to Pushover", e)
            return False

    def _build_pushover_payload(self, *, service: str, event_type: str, message: str) -> dict:
        priority = PUSHOVER_PRIORITY_MAP.get(event_type, "0")
        payload = {
            "token": self.config["pushover_app_token"],
            "user": self.config["pushover_user_key"],
            "title": f"PropertyOps: {service}",
            "message": message,
            "priority": priority,
        }
        if priority == "2":
            payload["retry"] = "60"
            payload["expire"] = "3600"
        return payload

    def _send_pushover(self, *, service: str, event_type: str, message: str) -> bool:
        """Send directly to Pushover API. Returns True on success."""
        token = self.config.get("pushover_app_token", "")
        user = self.config.get("pushover_user_key", "")
        if not token or not user:
            logger.warning("Pushover credentials not configured, cannot send fallback alert")
            return False

        payload = self._build_pushover_payload(
            service=service, event_type=event_type, message=message,
        )
        try:
            data = urlencode(payload).encode("utf-8")
            req = Request("https://api.pushover.net/1/messages.json", data=data, method="POST")
            with urlopen(req, timeout=self.config["request_timeout"]) as resp:
                resp.read()
            logger.info("Pushover alert sent: %s %s", service, event_type)
            return True
        except (URLError, HTTPError, OSError) as e:
            logger.error("Pushover alert failed: %s", e)
            return False
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add docker/scripts/healthmonitor.py docker/scripts/test_healthmonitor.py
git commit -m "feat: add NotificationManager with three-tier alerts"
```

---

### Task 2: ServiceConfig, ServiceState, and Health Check Logic

The core data structures and HTTP health checking — no restart logic yet.

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (add dataclasses and check logic)
- Modify: `docker/scripts/test_healthmonitor.py` (add tests)

- [ ] **Step 1: Write failing tests for health check logic**

Append to `docker/scripts/test_healthmonitor.py`:

```python
from healthmonitor import ServiceConfig, ServiceState, HealthMonitor


class TestServiceState(unittest.TestCase):
    def test_initial_state(self):
        state = ServiceState()
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.restart_count, 0)
        self.assertFalse(state.emergency)
        self.assertEqual(state.status, "healthy")

    def test_record_failure_increments(self):
        state = ServiceState()
        state.record_failure()
        self.assertEqual(state.consecutive_failures, 1)
        state.record_failure()
        self.assertEqual(state.consecutive_failures, 2)

    def test_record_recovery_resets(self):
        state = ServiceState()
        state.record_failure()
        state.record_failure()
        state.restart_count = 2
        state.emergency = True
        state.record_recovery()
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.restart_count, 0)
        self.assertFalse(state.emergency)
        self.assertEqual(state.status, "healthy")

    def test_is_in_cooldown(self):
        state = ServiceState()
        state.last_restart_time = time.time()
        self.assertTrue(state.is_in_cooldown(cooldown_period=300))

    def test_not_in_cooldown_after_period(self):
        state = ServiceState()
        state.last_restart_time = time.time() - 400
        self.assertFalse(state.is_in_cooldown(cooldown_period=300))

    def test_prune_restart_times(self):
        state = ServiceState()
        now = time.time()
        state.restart_times = [now - 2000, now - 1900, now - 100, now - 50]
        state.prune_restart_times(window=1800)
        self.assertEqual(len(state.restart_times), 2)


class TestServiceConfig(unittest.TestCase):
    def test_baserow_config(self):
        config = ServiceConfig(
            name="baserow",
            container_name="propertyops-baserow",
            internal_url="http://localhost:8086/api/_health/",
            public_url="https://app.jadepropertiesgroup.com/api/_health/",
            compose_file="/root/docker/baserow/docker-compose.yml",
            compose_service="baserow",
            dependencies=["postgres", "redis"],
        )
        self.assertEqual(config.name, "baserow")
        self.assertEqual(len(config.dependencies), 2)


class TestHealthCheck(unittest.TestCase):
    @patch("healthmonitor.urlopen")
    def test_check_url_healthy(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"ok"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = load_config()
        nm = NotificationManager(config)
        monitor = HealthMonitor(config, nm)
        result = monitor._check_url("http://localhost:8086/api/_health/")
        self.assertTrue(result)

    @patch("healthmonitor.urlopen")
    def test_check_url_unhealthy(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")

        config = load_config()
        nm = NotificationManager(config)
        monitor = HealthMonitor(config, nm)
        result = monitor._check_url("http://localhost:8086/api/_health/")
        self.assertFalse(result)

    @patch("healthmonitor.urlopen")
    def test_check_url_500_is_unhealthy(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "http://localhost:8086/api/_health/", 500, "Internal Server Error", {}, None
        )

        config = load_config()
        nm = NotificationManager(config)
        monitor = HealthMonitor(config, nm)
        result = monitor._check_url("http://localhost:8086/api/_health/")
        self.assertFalse(result)

    @patch.object(HealthMonitor, "_check_url")
    def test_classify_internal_failure(self, mock_check):
        mock_check.side_effect = [False, True]  # internal fails, public ok
        config = load_config()
        nm = NotificationManager(config)
        monitor = HealthMonitor(config, nm)
        svc = ServiceConfig(
            name="baserow",
            container_name="propertyops-baserow",
            internal_url="http://localhost:8086/api/_health/",
            public_url="https://app.jadepropertiesgroup.com/api/_health/",
            compose_file="/root/docker/baserow/docker-compose.yml",
            compose_service="baserow",
        )
        result = monitor.check_service(svc)
        self.assertEqual(result, "service_issue")

    @patch.object(HealthMonitor, "_check_url")
    def test_classify_tunnel_issue(self, mock_check):
        mock_check.side_effect = [True, False]  # internal ok, public fails
        config = load_config()
        nm = NotificationManager(config)
        monitor = HealthMonitor(config, nm)
        svc = ServiceConfig(
            name="baserow",
            container_name="propertyops-baserow",
            internal_url="http://localhost:8086/api/_health/",
            public_url="https://app.jadepropertiesgroup.com/api/_health/",
            compose_file="/root/docker/baserow/docker-compose.yml",
            compose_service="baserow",
        )
        result = monitor.check_service(svc)
        self.assertEqual(result, "tunnel_issue")

    @patch.object(HealthMonitor, "_check_url")
    def test_classify_healthy(self, mock_check):
        mock_check.side_effect = [True, True]  # both ok
        config = load_config()
        nm = NotificationManager(config)
        monitor = HealthMonitor(config, nm)
        svc = ServiceConfig(
            name="baserow",
            container_name="propertyops-baserow",
            internal_url="http://localhost:8086/api/_health/",
            public_url="https://app.jadepropertiesgroup.com/api/_health/",
            compose_file="/root/docker/baserow/docker-compose.yml",
            compose_service="baserow",
        )
        result = monitor.check_service(svc)
        self.assertEqual(result, "healthy")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py -v`
Expected: ImportError — `ServiceConfig`, `ServiceState`, `HealthMonitor` not defined.

- [ ] **Step 3: Implement ServiceConfig, ServiceState, and HealthMonitor.check_service**

Add to `docker/scripts/healthmonitor.py` after the NotificationManager class:

```python
# ── Service Configuration ────────────────────────────────────────────────────

@dataclass
class ServiceConfig:
    """Static configuration for a monitored service."""
    name: str
    container_name: str
    internal_url: str
    public_url: str
    compose_file: str
    compose_service: str
    dependencies: list = field(default_factory=list)


@dataclass
class ServiceState:
    """Mutable runtime state for a monitored service."""
    consecutive_failures: int = 0
    restart_count: int = 0
    restart_times: list = field(default_factory=list)
    last_restart_time: float = 0.0
    emergency: bool = False
    status: str = "healthy"  # healthy, unhealthy, restarting, emergency

    def record_failure(self):
        self.consecutive_failures += 1
        if self.status == "healthy":
            self.status = "unhealthy"

    def record_recovery(self):
        self.consecutive_failures = 0
        self.restart_count = 0
        self.restart_times.clear()
        self.emergency = False
        self.status = "healthy"

    def is_in_cooldown(self, cooldown_period: int) -> bool:
        if self.last_restart_time == 0:
            return False
        return (time.time() - self.last_restart_time) < cooldown_period

    def prune_restart_times(self, window: int):
        """Remove restart timestamps older than the rolling window."""
        cutoff = time.time() - window
        self.restart_times = [t for t in self.restart_times if t > cutoff]


# ── Service definitions ──────────────────────────────────────────────────────

def get_service_configs(compose_base: str) -> list:
    """Return the list of services to monitor."""
    return [
        ServiceConfig(
            name="baserow",
            container_name="propertyops-baserow",
            internal_url="http://localhost:8086/api/_health/",
            public_url="https://app.jadepropertiesgroup.com/api/_health/",
            compose_file=f"{compose_base}/baserow/docker-compose.yml",
            compose_service="baserow",
            dependencies=["postgres", "redis"],
        ),
        ServiceConfig(
            name="n8n",
            container_name="propertyops-n8n",
            internal_url="http://localhost:5678/healthz",
            public_url="https://automation.jadepropertiesgroup.com/healthz",
            compose_file=f"{compose_base}/n8n/docker-compose.yml",
            compose_service="n8n",
        ),
        ServiceConfig(
            name="docuseal",
            container_name="propertyops-docuseal",
            internal_url="http://localhost:3001/",
            public_url="",
            compose_file=f"{compose_base}/docuseal/docker-compose.yml",
            compose_service="docuseal",
        ),
    ]


# ── Health Monitor ───────────────────────────────────────────────────────────

class HealthMonitor:
    """Core monitoring loop: check services, manage state, trigger restarts."""

    def __init__(self, config: dict, notifications: NotificationManager):
        self.config = config
        self.notifications = notifications
        self.services = get_service_configs(config["compose_base"])
        self.states: dict[str, ServiceState] = {
            svc.name: ServiceState() for svc in self.services
        }

    def _check_url(self, url: str) -> bool:
        """Check if a URL returns HTTP 200 within timeout. Returns True if healthy."""
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=self.config["request_timeout"]) as resp:
                resp.read()
                return resp.status == 200
        except (URLError, HTTPError, OSError):
            return False

    def check_service(self, svc: ServiceConfig) -> str:
        """
        Check a service's internal and public endpoints.
        Returns: "healthy", "service_issue", or "tunnel_issue"
        """
        internal_ok = self._check_url(svc.internal_url)

        # Skip public check if no public URL configured
        if not svc.public_url:
            return "healthy" if internal_ok else "service_issue"

        public_ok = self._check_url(svc.public_url)

        if internal_ok and public_ok:
            return "healthy"
        elif internal_ok and not public_ok:
            return "tunnel_issue"
        else:
            # Internal failed (regardless of public) = service issue
            return "service_issue"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docker/scripts/healthmonitor.py docker/scripts/test_healthmonitor.py
git commit -m "feat: add ServiceConfig, ServiceState, and health check logic"
```

---

### Task 3: Restart Logic with Dependency Awareness

Implement the restart methods on HealthMonitor — including Baserow's dependency chain.

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (add restart methods to HealthMonitor)
- Modify: `docker/scripts/test_healthmonitor.py` (add tests)

- [ ] **Step 1: Write failing tests for restart logic**

Append to `docker/scripts/test_healthmonitor.py`:

```python
class TestRestartLogic(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.nm = NotificationManager(self.config)
        self.monitor = HealthMonitor(self.config, self.nm)

    @patch("healthmonitor.subprocess.run")
    def test_restart_simple_service(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        svc = self.monitor.services[1]  # n8n — no dependencies
        result = self.monitor._restart_service(svc)
        self.assertTrue(result)
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        self.assertIn("restart", call_args)
        self.assertIn("n8n", call_args)

    @patch("healthmonitor.subprocess.run")
    def test_restart_returns_false_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        svc = self.monitor.services[1]  # n8n
        result = self.monitor._restart_service(svc)
        self.assertFalse(result)

    @patch("healthmonitor.subprocess.run")
    @patch.object(HealthMonitor, "_check_dependency_health")
    def test_restart_baserow_checks_dependencies(self, mock_dep_check, mock_run):
        mock_dep_check.return_value = True  # dependencies healthy
        mock_run.return_value = MagicMock(returncode=0)
        svc = self.monitor.services[0]  # baserow — has dependencies
        result = self.monitor._restart_service(svc)
        self.assertTrue(result)
        mock_dep_check.assert_called_once()

    @patch("healthmonitor.subprocess.run")
    @patch.object(HealthMonitor, "_check_dependency_health")
    def test_restart_baserow_restarts_unhealthy_deps(self, mock_dep_check, mock_run):
        mock_dep_check.return_value = False  # deps unhealthy
        mock_run.return_value = MagicMock(returncode=0)
        svc = self.monitor.services[0]  # baserow
        self.monitor._restart_service(svc)
        # Should have called restart for dependency services + baserow
        self.assertTrue(mock_run.call_count >= 2)

    def test_should_restart_respects_threshold(self):
        svc = self.monitor.services[0]
        state = self.monitor.states["baserow"]
        state.consecutive_failures = 1
        self.assertFalse(self.monitor._should_restart(svc))
        state.consecutive_failures = 2
        self.assertTrue(self.monitor._should_restart(svc))

    def test_should_restart_respects_cooldown(self):
        svc = self.monitor.services[0]
        state = self.monitor.states["baserow"]
        state.consecutive_failures = 2
        state.last_restart_time = time.time()  # just restarted
        self.assertFalse(self.monitor._should_restart(svc))

    def test_should_restart_respects_max_restarts(self):
        svc = self.monitor.services[0]
        state = self.monitor.states["baserow"]
        state.consecutive_failures = 2
        state.restart_times = [time.time() - 100, time.time() - 50, time.time() - 10]
        state.prune_restart_times(window=self.config["restart_window"])
        self.assertFalse(self.monitor._should_restart(svc))

    def test_should_restart_false_when_emergency(self):
        svc = self.monitor.services[0]
        state = self.monitor.states["baserow"]
        state.consecutive_failures = 2
        state.emergency = True
        self.assertFalse(self.monitor._should_restart(svc))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py::TestRestartLogic -v`
Expected: AttributeError — `_restart_service`, `_should_restart` not defined.

- [ ] **Step 3: Implement restart methods**

Add to the `HealthMonitor` class in `docker/scripts/healthmonitor.py`:

```python
    def _should_restart(self, svc: ServiceConfig) -> bool:
        """Determine if a service should be restarted based on state and policy."""
        state = self.states[svc.name]

        if state.emergency:
            return False

        if state.consecutive_failures < self.config["failure_threshold"]:
            return False

        if state.is_in_cooldown(self.config["cooldown_period"]):
            return False

        state.prune_restart_times(window=self.config["restart_window"])
        if len(state.restart_times) >= self.config["max_restarts"]:
            # Entering emergency state
            state.emergency = True
            state.status = "emergency"
            logger.error("EMERGENCY: %s has exceeded max restarts (%d in %ds)",
                         svc.name, self.config["max_restarts"], self.config["restart_window"])
            self.notifications.notify(
                service=svc.name, status="emergency", event_type="emergency",
                message=f"{svc.name} failed {self.config['max_restarts']} restart attempts "
                        f"in {self.config['restart_window'] // 60} minutes. "
                        f"Manual intervention required.",
                restart_count=len(state.restart_times), check_type="internal",
            )
            return False

        return True

    def _restart_service(self, svc: ServiceConfig) -> bool:
        """Restart a service via docker compose. Returns True on success."""
        # Check dependencies first (Baserow depends on Postgres/Redis)
        if svc.dependencies:
            deps_ok = self._check_dependency_health(svc)
            if not deps_ok:
                logger.warning("Dependencies unhealthy for %s, restarting them first", svc.name)
                self._restart_dependencies(svc)

        logger.info("Restarting %s...", svc.name)
        result = subprocess.run(
            ["docker", "compose", "-f", svc.compose_file, "restart", svc.compose_service],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            logger.info("Restart command succeeded for %s", svc.name)
            return True
        else:
            logger.error("Restart command failed for %s: %s", svc.name, result.stderr)
            return False

    def _check_dependency_health(self, svc: ServiceConfig) -> bool:
        """Check if a service's Docker dependencies are healthy via docker inspect."""
        for dep in svc.dependencies:
            container_name = f"propertyops-{dep}"
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_name],
                    capture_output=True, text=True, timeout=10,
                )
                health = result.stdout.strip()
                if health != "healthy":
                    logger.warning("Dependency %s is %s", container_name, health)
                    return False
            except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                logger.warning("Could not check dependency %s: %s", container_name, e)
                return False
        return True

    def _restart_dependencies(self, svc: ServiceConfig):
        """Restart unhealthy dependencies for a service."""
        for dep in svc.dependencies:
            container_name = f"propertyops-{dep}"
            logger.info("Restarting dependency %s for %s...", dep, svc.name)
            subprocess.run(
                ["docker", "compose", "-f", svc.compose_file, "restart", dep],
                capture_output=True, text=True, timeout=120,
            )
            # Wait for dependency to become healthy
            for _ in range(24):  # 24 * 5s = 120s max wait
                try:
                    result = subprocess.run(
                        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_name],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.stdout.strip() == "healthy":
                        logger.info("Dependency %s is healthy", dep)
                        break
                except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                    pass
                time.sleep(5)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docker/scripts/healthmonitor.py docker/scripts/test_healthmonitor.py
git commit -m "feat: add restart logic with dependency awareness and cooldown"
```

---

### Task 4: Main Loop — Orchestrate Check, Restart, Notify Cycle

Wire everything together into the main monitoring loop with state transitions and notification deduplication.

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (add `run()` method and `main()`)
- Modify: `docker/scripts/test_healthmonitor.py` (add tests)

- [ ] **Step 1: Write failing tests for the main loop orchestration**

Append to `docker/scripts/test_healthmonitor.py`:

```python
class TestMainLoop(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.nm = NotificationManager(self.config)
        self.monitor = HealthMonitor(self.config, self.nm)

    @patch.object(NotificationManager, "notify")
    @patch.object(NotificationManager, "send_heartbeat")
    @patch.object(HealthMonitor, "check_service", return_value="healthy")
    def test_healthy_service_no_notification(self, mock_check, mock_heartbeat, mock_notify):
        self.monitor.process_service(self.monitor.services[0])
        mock_notify.assert_not_called()

    @patch.object(NotificationManager, "notify")
    @patch.object(HealthMonitor, "check_service", return_value="service_issue")
    def test_first_failure_no_restart(self, mock_check, mock_notify):
        """First failure: record it but don't restart (threshold=2)."""
        svc = self.monitor.services[0]
        self.monitor.process_service(svc)
        state = self.monitor.states[svc.name]
        self.assertEqual(state.consecutive_failures, 1)

    @patch.object(HealthMonitor, "_restart_service", return_value=True)
    @patch.object(NotificationManager, "notify")
    @patch.object(HealthMonitor, "check_service", return_value="service_issue")
    def test_second_failure_triggers_restart(self, mock_check, mock_notify, mock_restart):
        svc = self.monitor.services[0]
        self.monitor.states[svc.name].consecutive_failures = 1
        self.monitor.states[svc.name].status = "unhealthy"
        self.monitor.process_service(svc)
        mock_restart.assert_called_once_with(svc)

    @patch.object(NotificationManager, "notify")
    @patch.object(HealthMonitor, "check_service", return_value="tunnel_issue")
    def test_tunnel_issue_alerts_no_restart(self, mock_check, mock_notify):
        svc = self.monitor.services[0]
        self.monitor.process_service(svc)
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        self.assertEqual(call_kwargs["event_type"], "tunnel_issue")

    @patch.object(NotificationManager, "notify")
    @patch.object(HealthMonitor, "check_service", return_value="healthy")
    def test_recovery_sends_notification(self, mock_check, mock_notify):
        svc = self.monitor.services[0]
        state = self.monitor.states[svc.name]
        state.status = "unhealthy"
        state.consecutive_failures = 3
        self.monitor.process_service(svc)
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        self.assertEqual(call_kwargs["event_type"], "recovery")
        self.assertEqual(state.status, "healthy")

    @patch.object(NotificationManager, "notify")
    @patch.object(HealthMonitor, "check_service", return_value="healthy")
    def test_recovery_from_emergency_sends_notification(self, mock_check, mock_notify):
        svc = self.monitor.services[0]
        state = self.monitor.states[svc.name]
        state.status = "emergency"
        state.emergency = True
        state.consecutive_failures = 5
        self.monitor.process_service(svc)
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        self.assertEqual(call_kwargs["event_type"], "recovery")
        self.assertFalse(state.emergency)

    @patch.object(NotificationManager, "notify")
    @patch.object(HealthMonitor, "check_service", return_value="tunnel_issue")
    def test_tunnel_issue_deduplication(self, mock_check, mock_notify):
        """Only notify on first tunnel issue detection, not repeated ones."""
        svc = self.monitor.services[0]
        self.monitor.process_service(svc)
        self.assertEqual(mock_notify.call_count, 1)
        # Second call — same tunnel issue, should not re-notify
        self.monitor.process_service(svc)
        self.assertEqual(mock_notify.call_count, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py::TestMainLoop -v`
Expected: AttributeError — `process_service` not defined.

- [ ] **Step 3: Implement process_service and main loop**

Add to the `HealthMonitor` class in `docker/scripts/healthmonitor.py`:

```python
    def process_service(self, svc: ServiceConfig):
        """Process one check cycle for a single service."""
        state = self.states[svc.name]
        result = self.check_service(svc)

        if result == "healthy":
            if state.status != "healthy":
                # Recovery — was previously unhealthy/emergency
                prev_status = state.status
                state.record_recovery()
                logger.info("RECOVERED: %s (was %s)", svc.name, prev_status)
                self.notifications.notify(
                    service=svc.name, status="healthy", event_type="recovery",
                    message=f"{svc.name} has recovered and is healthy again.",
                    restart_count=0, check_type="internal",
                )
            return

        if result == "tunnel_issue":
            if state.status != "tunnel_issue":
                state.status = "tunnel_issue"
                logger.warning("TUNNEL ISSUE: %s — internal healthy but public unreachable", svc.name)
                self.notifications.notify(
                    service=svc.name, status="tunnel_issue", event_type="tunnel_issue",
                    message=f"{svc.name} is healthy internally but unreachable via public URL. "
                            f"Cloudflare tunnel may be down.",
                    check_type="public",
                )
            return

        # result == "service_issue"
        state.record_failure()
        logger.warning("FAILURE: %s — consecutive failures: %d",
                       svc.name, state.consecutive_failures)

        if self._should_restart(svc):
            state.status = "restarting"
            logger.info("RESTARTING: %s (attempt %d)", svc.name, len(state.restart_times) + 1)
            self.notifications.notify(
                service=svc.name, status="restarting", event_type="restart_initiated",
                message=f"{svc.name} failed {state.consecutive_failures} consecutive health checks. "
                        f"Restarting (attempt {len(state.restart_times) + 1}/{self.config['max_restarts']}).",
                restart_count=len(state.restart_times) + 1, check_type="internal",
            )

            success = self._restart_service(svc)
            now = time.time()
            state.last_restart_time = now
            state.restart_times.append(now)
            state.restart_count += 1

            if success:
                logger.info("Restart command succeeded for %s, will verify next cycle", svc.name)
                state.consecutive_failures = 0
                self.notifications.notify(
                    service=svc.name, status="restarting", event_type="restart_success",
                    message=f"{svc.name} restart command succeeded. Monitoring for recovery.",
                    restart_count=state.restart_count, check_type="internal",
                )
            else:
                logger.error("Restart command failed for %s", svc.name)
                self.notifications.notify(
                    service=svc.name, status="unhealthy", event_type="restart_failed",
                    message=f"{svc.name} restart command failed.",
                    restart_count=state.restart_count, check_type="internal",
                )

    def run(self):
        """Main monitoring loop. Runs until interrupted."""
        logger.info("PropertyOps Health Monitor starting...")
        logger.info("Monitoring %d services every %ds",
                     len(self.services), self.config["check_interval"])

        while True:
            for svc in self.services:
                try:
                    self.process_service(svc)
                except Exception as e:
                    logger.error("Unexpected error processing %s: %s", svc.name, e)

            self.notifications.send_heartbeat()

            time.sleep(self.config["check_interval"])
```

Add at the bottom of the file:

```python
# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    config = load_config()
    notifications = NotificationManager(config)
    monitor = HealthMonitor(config, notifications)
    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Health monitor stopped by user")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root && python3 -m pytest docker/scripts/test_healthmonitor.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docker/scripts/healthmonitor.py docker/scripts/test_healthmonitor.py
git commit -m "feat: add main monitoring loop with state transitions and deduplication"
```

---

### Task 5: Systemd Service Unit

Create the systemd unit file and verify it can be installed.

**Files:**
- Create: `/etc/systemd/system/propertyops-healthmonitor.service`

- [ ] **Step 1: Create the systemd unit file**

Create `/etc/systemd/system/propertyops-healthmonitor.service`:

```ini
[Unit]
Description=PropertyOps Service Health Monitor
Documentation=file:///root/docs/superpowers/specs/2026-04-07-service-health-monitor-design.md
After=docker.service
Wants=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/docker/scripts/healthmonitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=propertyops-healthmonitor

# Hardening
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/root/docker/logs
ReadOnlyPaths=/root/docker/scripts /root/docker/baserow /root/docker/n8n /root/docker/docuseal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Reload systemd and enable the service (do NOT start yet)**

```bash
systemctl daemon-reload
systemctl enable propertyops-healthmonitor.service
```

- [ ] **Step 3: Verify the unit is recognized**

Run: `systemctl status propertyops-healthmonitor.service`
Expected: Shows "loaded" and "inactive (dead)" — enabled but not yet started.

- [ ] **Step 4: Commit the unit file reference in the project**

Since the unit file lives in `/etc/systemd/system/` (not in the git repo), add a note to config.env:

Append to `docker/scripts/config.env`:

```bash
# ── Systemd service ─────────────────────────────────────────────────────────
# Unit file: /etc/systemd/system/propertyops-healthmonitor.service
# Start:  systemctl start propertyops-healthmonitor
# Status: systemctl status propertyops-healthmonitor
# Logs:   journalctl -u propertyops-healthmonitor -f
```

```bash
git add docker/scripts/config.env
git commit -m "feat: add systemd unit for health monitor"
```

---

### Task 6: Integration Test — Start Monitor and Verify

Start the service against real Docker services and verify it works end-to-end.

**Files:**
- No new files — validation only

- [ ] **Step 1: Start the health monitor**

```bash
systemctl start propertyops-healthmonitor
```

- [ ] **Step 2: Verify it's running and checking services**

```bash
journalctl -u propertyops-healthmonitor --no-pager -n 20
```

Expected: See log lines like:
```
PropertyOps Health Monitor starting...
Monitoring 3 services every 30s
```

- [ ] **Step 3: Verify log file is being written**

```bash
tail -20 /root/docker/logs/healthmonitor.log
```

Expected: Same log lines mirrored to file.

- [ ] **Step 4: Test notification fallback by checking Pushover directly**

This step requires Pushover credentials to be configured. If not yet configured, skip and note it for manual testing.

- [ ] **Step 5: Verify the service survives a restart**

```bash
systemctl restart propertyops-healthmonitor
systemctl status propertyops-healthmonitor
```

Expected: Active (running) after restart.

---

### Task 7: n8n Workflow — Health Alert Receiver

Build the n8n workflow that receives health alerts, logs to Baserow, and triggers LLM diagnosis on emergencies. This task produces a workflow specification for manual creation in n8n (n8n workflows are created via the UI, not code).

**Files:**
- Create: `docs/n8n-workflows/health-alert-workflow.md` (workflow spec for manual creation)

- [ ] **Step 1: Create the n8n workflow specification document**

Create `docs/n8n-workflows/health-alert-workflow.md`:

```markdown
# Health Alert Workflow — n8n Setup Guide

## Workflow: Service Health Monitor

### Trigger Node
- **Type:** Webhook
- **HTTP Method:** POST
- **Path:** `health-alert`
- **Authentication:** None (internal only, localhost)

### Node 1: Switch (Route by Event Type)
- **Field:** `{{ $json.event_type }}`
- **Rules:**
  - `emergency` → Branch 2 (LLM Diagnosis)
  - Default → Branch 1 (Log & Notify)

### Branch 1: Log & Notify

**Node 1a: Baserow — Create Row**
- **Table:** Service Health (create this table first — see schema below)
- **Fields mapping:**
  - Timestamp: `{{ $json.timestamp }}`
  - Service: `{{ $json.service }}`
  - Event Type: `{{ $json.event_type }}`
  - Check Type: `{{ $json.check_type }}`
  - Restart Count: `{{ $json.restart_count }}`
  - Message: `{{ $json.message }}`
  - Resolved: `false`

**Node 1b: Pushover — Send Notification**
- **Title:** `PropertyOps: {{ $json.service }}`
- **Message:** `{{ $json.message }}`
- **Priority:** Map from event_type:
  - emergency → 2 (with retry=60, expire=3600)
  - recovery → -1
  - all others → 0

### Branch 2: LLM Diagnosis (emergency only)

**Node 2a: Execute Command — Gather Logs**
- **Command:**
  ```bash
  echo "=== Docker Logs ===" && \
  docker logs --tail 200 propertyops-{{ $json.service }} 2>&1 && \
  echo "=== Container Inspect ===" && \
  docker inspect propertyops-{{ $json.service }} 2>&1 | python3 -c "import sys,json; d=json.load(sys.stdin)[0]; print(json.dumps({k: d[k] for k in ['State','HostConfig']}, indent=2))" && \
  echo "=== Disk Usage ===" && \
  df -h / /docker 2>/dev/null && \
  echo "=== Memory ===" && \
  free -m
  ```

**Node 2b: Baserow — Create Row** (same as 1a)

**Node 2c: HTTP Request — Claude API**
- **Method:** POST
- **URL:** `https://api.anthropic.com/v1/messages`
- **Headers:**
  - `x-api-key`: (from n8n credentials)
  - `anthropic-version`: `2023-06-01`
  - `content-type`: `application/json`
- **Body:**
  ```json
  {
    "model": "claude-sonnet-4-6-20250514",
    "max_tokens": 1024,
    "messages": [{
      "role": "user",
      "content": "You are a DevOps engineer diagnosing a Docker service failure. The service '{{ $json.service }}' has failed 3 restart attempts by our automated health monitor.\n\nHere are the logs and system state:\n\n{{ $node['Execute Command'].output }}\n\nWhat is likely wrong? Provide:\n1. Most probable root cause\n2. Top 3 remediation steps to try\n3. Any data that should be preserved before taking action\n\nBe concise and actionable."
    }]
  }
  ```

**Node 2d: Baserow — Update Row**
- **Row ID:** From Node 2b output
- **Field:** LLM Diagnosis = `{{ $json.content[0].text }}`

**Node 2e: Pushover — Send Emergency Alert**
- **Title:** `EMERGENCY: {{ $json.service }}`
- **Message:** `{{ $json.service }} failed 3 restarts.\n\nDiagnosis: {{ $node['Claude API'].json.content[0].text }}`
- **Priority:** 2 (retry=60, expire=3600)

## Baserow Table: Service Health

Create in Baserow with these fields:

| Field | Type | Options |
|-------|------|---------|
| Timestamp | DateTime | Include time |
| Service | Single Select | Baserow, n8n, DocuSeal |
| Event Type | Single Select | failure_detected, restart_initiated, restart_success, restart_failed, emergency, recovery, tunnel_issue |
| Check Type | Single Select | internal, public |
| Restart Count | Number | Integer |
| Message | Long Text | |
| LLM Diagnosis | Long Text | |
| Resolved | Boolean | Default: false |
| Resolution Notes | Long Text | |
```

- [ ] **Step 2: Commit**

```bash
git add docs/n8n-workflows/health-alert-workflow.md
git commit -m "docs: add n8n health alert workflow specification"
```

- [ ] **Step 3: Create the Baserow "Service Health" table**

Manually create the table in Baserow following the schema above. This cannot be automated via code.

- [ ] **Step 4: Build the n8n workflow**

Manually build the workflow in n8n following the specification above. Test by sending a manual POST:

```bash
curl -X POST http://localhost:5678/webhook/health-alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "test",
    "status": "unhealthy",
    "event_type": "restart_initiated",
    "message": "Test alert from manual curl",
    "timestamp": "2026-04-07T12:00:00Z",
    "restart_count": 1,
    "check_type": "internal"
  }'
```

Expected: Row appears in Baserow "Service Health" table, Pushover notification received.

- [ ] **Step 5: Test emergency branch with LLM diagnosis**

```bash
curl -X POST http://localhost:5678/webhook/health-alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "baserow",
    "status": "emergency",
    "event_type": "emergency",
    "message": "Baserow failed 3 restart attempts",
    "timestamp": "2026-04-07T12:00:00Z",
    "restart_count": 3,
    "check_type": "internal"
  }'
```

Expected: Row in Baserow with LLM Diagnosis field populated, emergency Pushover alert with diagnosis.

---

### Task 8: Uptime Kuma Push Monitor Setup

Configure the Uptime Kuma push monitor and update config.env with the URL.

**Files:**
- Modify: `docker/scripts/config.env` (add Uptime Kuma URL once created)

- [ ] **Step 1: Create a Push monitor in Uptime Kuma**

In Uptime Kuma UI:
1. Add New Monitor
2. Type: **Push**
3. Friendly Name: "PropertyOps Health Monitor Heartbeat"
4. Heartbeat Interval: **60 seconds** (monitor checks every 30s, so 60s gives a buffer)
5. Retries: **2** (wait for 2 missed heartbeats before alerting)
6. Copy the push URL

- [ ] **Step 2: Update config.env with the Uptime Kuma push URL**

Edit `docker/scripts/config.env`, set the `UPTIME_KUMA_PUSH_URL` value to the URL from step 1.

- [ ] **Step 3: Restart the health monitor to pick up new config**

```bash
systemctl restart propertyops-healthmonitor
```

- [ ] **Step 4: Verify heartbeats are arriving in Uptime Kuma**

Check Uptime Kuma UI — the push monitor should show "Up" within 30-60 seconds.

- [ ] **Step 5: Commit config change**

```bash
git add docker/scripts/config.env
git commit -m "feat: configure Uptime Kuma heartbeat URL"
```
