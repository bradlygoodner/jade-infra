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
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
import ssl

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


# ── Process / FD inspection helpers ──────────────────────────────────────────

def walk_process_tree(root_pid: int, proc_root: str = "/proc") -> list[int]:
    """Return all PIDs in the descendant tree rooted at root_pid (inclusive).

    Reads /proc/<pid>/task/<pid>/children which lists direct child PIDs as
    space-separated values. Recurses through children. Tolerates PIDs that
    disappear mid-walk (worker exits between readdir and read).
    """
    pids: list[int] = []
    seen: set[int] = set()
    queue: list[int] = [root_pid]
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pids.append(pid)
        children_path = Path(proc_root) / str(pid) / "task" / str(pid) / "children"
        try:
            children_str = children_path.read_text().strip()
        except (FileNotFoundError, NotADirectoryError, PermissionError, ProcessLookupError):
            continue
        if not children_str:
            continue
        for child in children_str.split():
            try:
                queue.append(int(child))
            except ValueError:
                continue
    return pids


def count_open_fds(pids: list[int], proc_root: str = "/proc") -> int:
    """Return the total number of open file descriptors across the given PIDs.

    Reads /proc/<pid>/fd/ for each PID and counts entries. Tolerates PIDs that
    have already exited (FileNotFoundError) — those contribute 0.
    """
    total = 0
    for pid in pids:
        fd_dir = Path(proc_root) / str(pid) / "fd"
        try:
            with os.scandir(fd_dir) as entries:
                total += sum(1 for _ in entries)
        except (FileNotFoundError, NotADirectoryError, PermissionError, ProcessLookupError):
            continue
    return total


def read_soft_nofile_limit(pid: int, proc_root: str = "/proc") -> int:
    """Return the soft `nofile` rlimit for a PID by parsing /proc/<pid>/limits.

    Raises ValueError if the "Max open files" line is not found.
    """
    limits_path = Path(proc_root) / str(pid) / "limits"
    for line in limits_path.read_text().splitlines():
        if line.startswith("Max open files"):
            # Format: "Max open files            <soft>                <hard>                files"
            parts = line.split()
            return int(parts[3])
    raise ValueError(f"Max open files line not found in {limits_path}")


def get_container_pid(container_name: str) -> int | None:
    """Return the host PID of a running container, or None if not running / unreachable.

    Uses `docker inspect`. Returns None on:
      - non-zero exit code (container missing)
      - "0" stdout (Docker's signal that the container exists but isn't running)
      - empty stdout
      - subprocess timeout / OSError
    """
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Pid}}", container_name],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        logger.warning("docker inspect failed for %s: %s", container_name, e)
        return None
    if result.returncode != 0:
        return None
    pid_str = result.stdout.strip()
    if not pid_str or pid_str == "0":
        return None
    try:
        return int(pid_str)
    except ValueError:
        logger.warning("docker inspect returned non-integer PID for %s: %r", container_name, pid_str)
        return None


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

    fd_warn = int(raw.get("FD_WARN_PERCENT", "40"))
    fd_crit = int(raw.get("FD_CRITICAL_PERCENT", "60"))
    if not (0 < fd_warn < fd_crit <= 100):
        raise ValueError(
            f"Invalid FD threshold config: FD_WARN_PERCENT={fd_warn}, "
            f"FD_CRITICAL_PERCENT={fd_crit} (require 0 < warn < critical <= 100)"
        )

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
        "compose_base": raw.get("COMPOSE_BASE", "/opt/jade-infra/docker"),
        "fd_warn_percent": fd_warn,
        "fd_critical_percent": fd_crit,
    }


# ── Notification Manager ────────────────────────────────────────────────────

PUSHOVER_PRIORITY_MAP = {
    "failure_detected": "0",
    "restart_initiated": "0",
    "restart_success": "0",
    "restart_failed": "0",
    "tunnel_issue": "0",
    "fd_warning": "0",
    "fd_critical": "1",
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
            # Allow self-signed certs for internal LAN Uptime Kuma instances
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urlopen(req, timeout=5, context=ctx) as resp:
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
        except Exception as e:
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
    host_header: str = ""  # Override Host header for internal checks (needed when Caddy routes by hostname)


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

    def is_in_grace_period(self, grace_period: int = 90) -> bool:
        """Check if service is still in post-restart grace period (boot time)."""
        if self.last_restart_time == 0:
            return False
        return (time.time() - self.last_restart_time) < grace_period

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
            host_header="app.jadepropertiesgroup.com",
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
        self.fd_monitor = FDMonitor(config, notifications, self)

    def _check_url(self, url: str, host_header: str = "") -> bool:
        """Check if a URL returns HTTP 200 within timeout. Returns True if healthy."""
        try:
            req = Request(url, method="GET")
            req.add_header("User-Agent", "PropertyOps-HealthMonitor/1.0")
            if host_header:
                req.add_header("Host", host_header)
            with urlopen(req, timeout=self.config["request_timeout"]) as resp:
                resp.read()
                return resp.status == 200
        except Exception:
            return False

    def check_service(self, svc: ServiceConfig) -> str:
        """
        Check a service's internal and public endpoints.
        Returns: "healthy", "service_issue", or "tunnel_issue"
        """
        internal_ok = self._check_url(svc.internal_url, host_header=svc.host_header)

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

    def _check_restart_budget(self, svc: ServiceConfig) -> bool:
        """Return True if a restart is permitted right now (cooldown ok, budget ok, not in emergency).

        Mutates state: enters emergency if max_restarts exceeded within the rolling window.
        Does NOT consider whether the service appears broken — that's the caller's job.
        """
        state = self.states[svc.name]

        if state.emergency:
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

    def _should_restart(self, svc: ServiceConfig) -> bool:
        """Failure-driven path: only restart after `failure_threshold` consecutive failures."""
        state = self.states[svc.name]
        if state.consecutive_failures < self.config["failure_threshold"]:
            return False
        return self._check_restart_budget(svc)

    def request_restart(self, svc: ServiceConfig, reason: str) -> bool:
        """Attempt to restart a service for any reason (FD critical, manual trigger, etc.).

        Reuses the same budget guard as the failure-driven path so callers can't
        bypass max_restarts/cooldown/emergency. Returns True if the restart command
        was issued AND succeeded, False otherwise.

        The `reason` string is included in the restart_initiated notification so
        operators see *why* a restart fired (vs. "n consecutive failures").
        """
        # NOTE: _check_restart_budget can mutate state — it enters emergency and
        # fires an emergency notification if max_restarts is exhausted within the
        # rolling window. That's intentional; we don't double-notify here.
        if not self._check_restart_budget(svc):
            return False

        state = self.states[svc.name]
        state.status = "restarting"
        attempt = len(state.restart_times) + 1
        logger.info("RESTARTING: %s (attempt %d) — reason: %s", svc.name, attempt, reason)
        self.notifications.notify(
            service=svc.name, status="restarting", event_type="restart_initiated",
            message=f"{svc.name} restart triggered: {reason}. "
                    f"Attempt {attempt}/{self.config['max_restarts']}.",
            restart_count=attempt, check_type="internal",
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
        return success

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
        if state.is_in_grace_period():
            logger.info("GRACE PERIOD: %s — ignoring failure during post-restart boot", svc.name)
            return

        state.record_failure()
        logger.warning("FAILURE: %s — consecutive failures: %d",
                       svc.name, state.consecutive_failures)

        if self._should_restart(svc):
            self.request_restart(
                svc,
                reason=f"{state.consecutive_failures} consecutive health check failures",
            )

    # ── Disk space monitoring ──────────────────────────────────────────────────

    DISK_CHECK_PATHS = [
        ("/", "root filesystem"),
        ("/docker/volumes/propertyops", "Baserow data"),
    ]
    DISK_WARN_PERCENT = 80
    DISK_CRITICAL_PERCENT = 90
    _disk_alert_sent: dict = {}

    def check_disk_space(self):
        """Check disk usage on critical mount points. Alert if thresholds exceeded."""
        for path, label in self.DISK_CHECK_PATHS:
            try:
                usage = shutil.disk_usage(path)
                pct = (usage.used / usage.total) * 100
                free_gb = usage.free / (1024 ** 3)

                if pct >= self.DISK_CRITICAL_PERCENT:
                    level = "critical"
                elif pct >= self.DISK_WARN_PERCENT:
                    level = "warning"
                else:
                    # Clear alert state on recovery
                    if path in self._disk_alert_sent:
                        del self._disk_alert_sent[path]
                    continue

                # Only alert once per level per path (until recovered)
                if self._disk_alert_sent.get(path) == level:
                    continue

                self._disk_alert_sent[path] = level
                msg = (f"Disk space {level}: {label} ({path}) is {pct:.1f}% full. "
                       f"{free_gb:.1f} GB remaining.")
                logger.warning(msg)
                self.notifications.notify(
                    service="system", status=level,
                    event_type="emergency" if level == "critical" else "failure_detected",
                    message=msg, check_type="disk",
                )
            except OSError as e:
                logger.warning("Could not check disk space for %s: %s", path, e)

    def run(self):
        """Main monitoring loop. Runs until interrupted."""
        logger.info("PropertyOps Health Monitor starting...")
        logger.info("Monitoring %d services every %ds",
                     len(self.services), self.config["check_interval"])

        cycle = 0
        while True:
            for svc in self.services:
                try:
                    self.process_service(svc)
                except Exception as e:
                    logger.error("Unexpected error processing %s: %s", svc.name, e)
                # FD probe: baserow only in v1
                if svc.name == "baserow":
                    try:
                        self.fd_monitor.check(svc)
                    except Exception as e:
                        logger.error("Unexpected error in FD probe for %s: %s", svc.name, e)

            # Check disk space every 10 cycles (~5 minutes at 30s interval)
            cycle += 1
            if cycle % 10 == 0:
                try:
                    self.check_disk_space()
                except Exception as e:
                    logger.error("Unexpected error checking disk space: %s", e)

            self.notifications.send_heartbeat()

            time.sleep(self.config["check_interval"])


# ── FD Monitor ────────────────────────────────────────────────────────────────

@dataclass
class FDMonitorState:
    """Per-service mutable state for FDMonitor."""
    last_warning_fired: bool = False
    last_critical_fired: bool = False
    last_alert_count: int = 0


class FDMonitor:
    """Probes baserow's process-tree FD count and triggers warnings/restarts.

    Uses the soft `nofile` rlimit as the denominator for percent calculations.
    Reuses HealthMonitor.request_restart() for the critical path so this
    code path inherits cooldown/budget/emergency semantics.
    """

    HYSTERESIS_CLEAR_PERCENT = 50
    IMPLAUSIBLY_LOW_FD_COUNT = 10

    def __init__(self, config: dict, notifications: NotificationManager, health_monitor: HealthMonitor):
        self.config = config
        self.notifications = notifications
        self.health_monitor = health_monitor
        self.states: dict[str, FDMonitorState] = {}

    def check(self, svc: ServiceConfig) -> None:
        """Sample FD usage for svc and fire alerts / restart per thresholds."""
        state = self.states.setdefault(svc.name, FDMonitorState())

        root_pid = get_container_pid(svc.container_name)
        if root_pid is None:
            logger.debug("FDMonitor: %s container not running, skipping FD check", svc.name)
            return

        try:
            pids = walk_process_tree(root_pid)
            count = count_open_fds(pids)
            soft_limit = read_soft_nofile_limit(root_pid)
        except (OSError, ValueError) as e:
            logger.warning("FDMonitor: failed to read FD state for %s: %s", svc.name, e)
            return

        if soft_limit <= 0:
            logger.warning("FDMonitor: implausible soft_limit=%d for %s, skipping", soft_limit, svc.name)
            return

        if count < self.IMPLAUSIBLY_LOW_FD_COUNT:
            logger.debug("FDMonitor: %s FD count %d implausibly low, skipping", svc.name, count)
            return

        warn_threshold = soft_limit * self.config["fd_warn_percent"] // 100
        critical_threshold = soft_limit * self.config["fd_critical_percent"] // 100
        clear_threshold = soft_limit * self.HYSTERESIS_CLEAR_PERCENT // 100

        pct = count * 100 // soft_limit
        logger.debug("FDMonitor: %s FDs=%d soft=%d (%d%%)", svc.name, count, soft_limit, pct)

        # Reset critical hysteresis as soon as count is below critical threshold,
        # so a re-excursion above critical re-alerts even if FDs never dipped to
        # the recovery (50%) threshold.
        if count < critical_threshold:
            state.last_critical_fired = False

        if count >= critical_threshold:
            if not state.last_critical_fired:
                self._fire_critical(svc, count, soft_limit, pct)
                state.last_critical_fired = True
            state.last_alert_count = max(state.last_alert_count, count)
            state.last_warning_fired = True  # track so recovery fires when count drops
            ok = self.health_monitor.request_restart(
                svc, reason=f"FD count {count}/{soft_limit} ({pct}%)"
            )
            if not ok:
                logger.warning(
                    "FDMonitor: restart suppressed for %s (budget/cooldown/emergency); "
                    "FD count %d/%d (%d%%)",
                    svc.name, count, soft_limit, pct,
                )
        elif count >= warn_threshold:
            if not state.last_warning_fired:
                self._fire_warning(svc, count, soft_limit, pct)
                state.last_warning_fired = True
                state.last_alert_count = count
            else:
                state.last_alert_count = max(state.last_alert_count, count)
        elif count < clear_threshold and state.last_warning_fired:
            self._fire_recovery(svc, state.last_alert_count, soft_limit)
            state.last_warning_fired = False
            state.last_critical_fired = False
            state.last_alert_count = 0

    def _fire_warning(self, svc: ServiceConfig, count: int, soft_limit: int, pct: int) -> None:
        msg = (f"{svc.name} FD count is {count}/{soft_limit} ({pct}%) — "
               f"warning threshold ({self.config['fd_warn_percent']}%) crossed.")
        logger.warning(msg)
        self.notifications.notify(
            service=svc.name, status="unhealthy", event_type="fd_warning",
            message=msg, check_type="fd",
        )

    def _fire_critical(self, svc: ServiceConfig, count: int, soft_limit: int, pct: int) -> None:
        msg = (f"{svc.name} FD count is {count}/{soft_limit} ({pct}%) — "
               f"critical threshold ({self.config['fd_critical_percent']}%) crossed; "
               f"requesting restart.")
        logger.error(msg)
        self.notifications.notify(
            service=svc.name, status="unhealthy", event_type="fd_critical",
            message=msg, check_type="fd",
        )

    def _fire_recovery(self, svc: ServiceConfig, peak_count: int, soft_limit: int) -> None:
        peak_pct = peak_count * 100 // soft_limit if soft_limit else 0
        msg = (f"{svc.name} FD count recovered (peak was {peak_count}/{soft_limit} "
               f"= {peak_pct}%).")
        logger.info(msg)
        self.notifications.notify(
            service=svc.name, status="healthy", event_type="recovery",
            message=msg, check_type="fd",
        )


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
