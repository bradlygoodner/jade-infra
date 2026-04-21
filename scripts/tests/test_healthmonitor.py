"""Tests for healthmonitor.py."""


def test_module_imports():
    """Smoke test: the module imports without error."""
    import healthmonitor
    assert healthmonitor.__name__ == "healthmonitor"


def _make_fake_proc(tmp_path, tree):
    """Build a fake /proc tree.

    `tree` is {parent_pid: [child_pids]}. Each PID gets /proc/<pid>/task/<pid>/children
    populated with space-separated children PIDs (kernel format).
    """
    for pid, children in tree.items():
        task_dir = tmp_path / str(pid) / "task" / str(pid)
        task_dir.mkdir(parents=True, exist_ok=True)
        children_str = " ".join(str(c) for c in children) + ("\n" if children else "")
        (task_dir / "children").write_text(children_str)
    return tmp_path


def test_walk_process_tree_single_pid(tmp_path):
    from healthmonitor import walk_process_tree
    proc = _make_fake_proc(tmp_path, {100: []})
    assert walk_process_tree(100, proc_root=str(proc)) == [100]


def test_walk_process_tree_with_children(tmp_path):
    from healthmonitor import walk_process_tree
    proc = _make_fake_proc(tmp_path, {
        100: [101, 102],
        101: [],
        102: [103],
        103: [],
    })
    pids = walk_process_tree(100, proc_root=str(proc))
    assert sorted(pids) == [100, 101, 102, 103]


def test_walk_process_tree_missing_pid_skipped(tmp_path):
    """If a PID disappears mid-walk, the walker tolerates it."""
    from healthmonitor import walk_process_tree
    proc = _make_fake_proc(tmp_path, {
        100: [101, 999],  # 999 doesn't exist in /proc
        101: [],
    })
    pids = walk_process_tree(100, proc_root=str(proc))
    assert sorted(pids) == [100, 101, 999]  # 999 is in the list, just has no children to recurse into


def _add_fake_fds(tmp_path, pid, n_fds):
    """Populate /proc/<pid>/fd/ with n_fds entries (mimicking open file descriptors)."""
    fd_dir = tmp_path / str(pid) / "fd"
    fd_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_fds):
        (fd_dir / str(i)).touch()


def test_count_open_fds_single_pid(tmp_path):
    from healthmonitor import count_open_fds
    _add_fake_fds(tmp_path, 100, 5)
    assert count_open_fds([100], proc_root=str(tmp_path)) == 5


def test_count_open_fds_multiple_pids(tmp_path):
    from healthmonitor import count_open_fds
    _add_fake_fds(tmp_path, 100, 5)
    _add_fake_fds(tmp_path, 101, 3)
    _add_fake_fds(tmp_path, 102, 7)
    assert count_open_fds([100, 101, 102], proc_root=str(tmp_path)) == 15


def test_count_open_fds_missing_pid_returns_zero_for_that_pid(tmp_path):
    from healthmonitor import count_open_fds
    _add_fake_fds(tmp_path, 100, 5)
    # PID 999 has no /proc entry — should be silently skipped
    assert count_open_fds([100, 999], proc_root=str(tmp_path)) == 5


LIMITS_FIXTURE = """\
Limit                     Soft Limit           Hard Limit           Units
Max cpu time              unlimited            unlimited            seconds
Max file size             unlimited            unlimited            bytes
Max data size             unlimited            unlimited            bytes
Max stack size            8388608              unlimited            bytes
Max core file size        0                    unlimited            bytes
Max resident set          unlimited            unlimited            bytes
Max processes             63474                63474                processes
Max open files            65535                65535                files
Max locked memory         8388608              8388608              bytes
Max address space         unlimited            unlimited            bytes
Max file locks            unlimited            unlimited            locks
Max pending signals       63474                63474                signals
Max msgqueue size         819200               819200               bytes
Max nice priority         0                    0
Max realtime priority     0                    0
Max realtime timeout      unlimited            unlimited            us
"""


def test_read_soft_nofile_limit(tmp_path):
    from healthmonitor import read_soft_nofile_limit
    pid_dir = tmp_path / "100"
    pid_dir.mkdir()
    (pid_dir / "limits").write_text(LIMITS_FIXTURE)
    assert read_soft_nofile_limit(100, proc_root=str(tmp_path)) == 65535


def test_read_soft_nofile_limit_default_1024(tmp_path):
    """Verify parser handles the default Linux 1024 soft limit too."""
    from healthmonitor import read_soft_nofile_limit
    fixture = LIMITS_FIXTURE.replace(
        "Max open files            65535                65535",
        "Max open files            1024                 524288",
    )
    pid_dir = tmp_path / "100"
    pid_dir.mkdir()
    (pid_dir / "limits").write_text(fixture)
    assert read_soft_nofile_limit(100, proc_root=str(tmp_path)) == 1024


def test_read_soft_nofile_limit_missing_line_raises(tmp_path):
    from healthmonitor import read_soft_nofile_limit
    pid_dir = tmp_path / "100"
    pid_dir.mkdir()
    (pid_dir / "limits").write_text("Limit Soft Hard Units\nMax cpu time unlimited unlimited seconds\n")
    import pytest
    with pytest.raises(ValueError, match="Max open files"):
        read_soft_nofile_limit(100, proc_root=str(tmp_path))


def test_get_container_pid_success(monkeypatch):
    from healthmonitor import get_container_pid
    import subprocess

    class FakeResult:
        returncode = 0
        stdout = "578332\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    assert get_container_pid("propertyops-baserow") == 578332


def test_get_container_pid_container_not_running(monkeypatch):
    from healthmonitor import get_container_pid
    import subprocess

    class FakeResult:
        returncode = 0
        stdout = "0\n"  # Docker returns "0" if container is stopped
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    assert get_container_pid("propertyops-baserow") is None


def test_get_container_pid_inspect_fails(monkeypatch):
    from healthmonitor import get_container_pid
    import subprocess

    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "Error: No such container"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    assert get_container_pid("propertyops-baserow") is None


def test_get_container_pid_subprocess_timeout(monkeypatch):
    from healthmonitor import get_container_pid
    import subprocess

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=10)

    monkeypatch.setattr(subprocess, "run", boom)
    assert get_container_pid("propertyops-baserow") is None


def _make_test_health_monitor():
    """Construct a HealthMonitor with deterministic config and no real notifications."""
    from healthmonitor import HealthMonitor, NotificationManager
    config = {
        "n8n_webhook_base": "http://nowhere/webhook",
        "pushover_app_token": "",
        "pushover_user_key": "",
        "uptime_kuma_push_url": "",
        "check_interval": 30,
        "failure_threshold": 2,
        "cooldown_period": 300,
        "max_restarts": 3,
        "request_timeout": 10,
        "restart_window": 1800,
        "compose_base": "/tmp/nonexistent",
        "fd_warn_percent": 70,
        "fd_critical_percent": 90,
    }
    notifications = NotificationManager(config)
    return HealthMonitor(config, notifications)


def test_check_restart_budget_allows_when_clean():
    hm = _make_test_health_monitor()
    svc = hm.services[0]  # baserow
    assert hm._check_restart_budget(svc) is True


def test_check_restart_budget_blocks_when_in_emergency():
    hm = _make_test_health_monitor()
    svc = hm.services[0]
    hm.states[svc.name].emergency = True
    assert hm._check_restart_budget(svc) is False


def test_check_restart_budget_blocks_when_in_cooldown():
    import time
    hm = _make_test_health_monitor()
    svc = hm.services[0]
    hm.states[svc.name].last_restart_time = time.time()  # just restarted
    assert hm._check_restart_budget(svc) is False


def test_check_restart_budget_enters_emergency_when_max_restarts_exceeded():
    import time
    hm = _make_test_health_monitor()
    svc = hm.services[0]
    state = hm.states[svc.name]
    now = time.time()
    state.restart_times = [now - 60, now - 30, now - 10]  # 3 recent restarts
    assert hm._check_restart_budget(svc) is False
    assert state.emergency is True
    assert state.status == "emergency"


def test_request_restart_blocked_by_budget(monkeypatch):
    """request_restart respects _check_restart_budget."""
    hm = _make_test_health_monitor()
    svc = next(s for s in hm.services if s.name == "baserow")
    hm.states[svc.name].emergency = True

    called = []
    monkeypatch.setattr(hm, "_restart_service", lambda s: called.append(s) or True)

    result = hm.request_restart(svc, reason="test")
    assert result is False
    assert called == []  # restart not attempted


def test_request_restart_executes_when_allowed(monkeypatch):
    """request_restart calls _restart_service and updates state when budget allows."""
    hm = _make_test_health_monitor()
    svc = next(s for s in hm.services if s.name == "baserow")

    called_restart = []
    monkeypatch.setattr(hm, "_restart_service", lambda s: called_restart.append(s) or True)
    monkeypatch.setattr(hm.notifications, "notify", lambda **kw: None)

    result = hm.request_restart(svc, reason="FD count 59000/65535 (90%)")
    assert result is True
    assert len(called_restart) == 1
    state = hm.states[svc.name]
    assert state.restart_count == 1
    assert len(state.restart_times) == 1
    assert state.consecutive_failures == 0  # reset after successful restart


def test_request_restart_handles_restart_failure(monkeypatch):
    """If _restart_service returns False, state still records the attempt."""
    hm = _make_test_health_monitor()
    svc = next(s for s in hm.services if s.name == "baserow")

    monkeypatch.setattr(hm, "_restart_service", lambda s: False)
    monkeypatch.setattr(hm.notifications, "notify", lambda **kw: None)

    result = hm.request_restart(svc, reason="test")
    assert result is False
    state = hm.states[svc.name]
    assert state.restart_count == 1
    assert len(state.restart_times) == 1  # attempt counted toward budget even on failure


def test_process_service_failure_driven_restart_uses_request_restart(monkeypatch):
    """When consecutive_failures crosses threshold, process_service calls request_restart with a useful reason."""
    hm = _make_test_health_monitor()
    svc = next(s for s in hm.services if s.name == "baserow")

    # Force the health check to report service_issue
    monkeypatch.setattr(hm, "check_service", lambda s: "service_issue")

    # Capture request_restart calls
    calls = []
    monkeypatch.setattr(hm, "request_restart", lambda s, reason: calls.append((s.name, reason)) or True)

    # First failure — below threshold (failure_threshold=2 in test config), no restart
    hm.process_service(svc)
    assert calls == []
    assert hm.states[svc.name].consecutive_failures == 1

    # Second failure — threshold met, restart requested
    hm.process_service(svc)
    assert len(calls) == 1
    name, reason = calls[0]
    assert name == "baserow"
    assert "consecutive" in reason.lower() or "failures" in reason.lower()


def test_process_service_request_restart_call_uses_real_notification_signature(monkeypatch):
    """Defense against kwarg typos: when process_service triggers request_restart end-to-end,
    the resulting notify() calls must use kwargs that match NotificationManager.notify's signature.
    Captures the kwargs and asserts the expected event_types fire in order.
    """
    hm = _make_test_health_monitor()
    svc = next(s for s in hm.services if s.name == "baserow")

    monkeypatch.setattr(hm, "check_service", lambda s: "service_issue")
    monkeypatch.setattr(hm, "_restart_service", lambda s: True)

    notify_calls = []
    monkeypatch.setattr(hm.notifications, "notify",
                        lambda **kw: notify_calls.append(kw) or None)

    # Cross failure_threshold (2 failures)
    hm.process_service(svc)
    hm.process_service(svc)

    events = [c["event_type"] for c in notify_calls]
    assert "restart_initiated" in events
    assert "restart_success" in events
    initiated = next(c for c in notify_calls if c["event_type"] == "restart_initiated")
    assert "consecutive" in initiated["message"].lower() or "failures" in initiated["message"].lower()


def test_load_config_fd_defaults(tmp_path, monkeypatch):
    """load_config provides sensible defaults for FD thresholds when keys absent."""
    import healthmonitor
    from healthmonitor import load_config
    # load_config resolves config.env via Path(__file__).parent / "config.env".
    # Point healthmonitor.__file__ at a fake path under tmp_path so the lookup
    # finds OUR test config.env, not the real one on disk.
    (tmp_path / "config.env").write_text("CHECK_INTERVAL=30\n")
    monkeypatch.setattr(healthmonitor, "__file__", str(tmp_path / "healthmonitor.py"))
    cfg = load_config()
    assert cfg["fd_warn_percent"] == 40
    assert cfg["fd_critical_percent"] == 60


def test_load_config_fd_custom_values(tmp_path, monkeypatch):
    """load_config reads custom FD thresholds from config.env."""
    import healthmonitor
    from healthmonitor import load_config
    (tmp_path / "config.env").write_text("FD_WARN_PERCENT=60\nFD_CRITICAL_PERCENT=85\n")
    monkeypatch.setattr(healthmonitor, "__file__", str(tmp_path / "healthmonitor.py"))
    cfg = load_config()
    assert cfg["fd_warn_percent"] == 60
    assert cfg["fd_critical_percent"] == 85


class _FakeNotifications:
    """Captures notify() calls for assertion."""
    def __init__(self):
        self.calls = []

    def notify(self, **kw):
        self.calls.append(kw)


class _FakeHealthMonitor:
    """Stub for HealthMonitor — only needs request_restart."""
    def __init__(self):
        self.restart_calls = []

    def request_restart(self, svc, reason):
        self.restart_calls.append((svc.name, reason))
        return True


def _make_baserow_svc():
    from healthmonitor import ServiceConfig
    return ServiceConfig(
        name="baserow",
        container_name="propertyops-baserow",
        internal_url="http://localhost:8086/api/_health/",
        public_url="https://app.example.com/api/_health/",
        compose_file="/tmp/docker-compose.yml",
        compose_service="baserow",
    )


def _patch_fd_state(monkeypatch, *, root_pid, fd_count, soft_limit):
    """Patch the four module-level FD-probe helpers to return controlled values."""
    import healthmonitor
    monkeypatch.setattr(healthmonitor, "get_container_pid", lambda name: root_pid)
    monkeypatch.setattr(healthmonitor, "walk_process_tree", lambda pid, **kw: [pid])
    monkeypatch.setattr(healthmonitor, "count_open_fds", lambda pids, **kw: fd_count)
    monkeypatch.setattr(healthmonitor, "read_soft_nofile_limit", lambda pid, **kw: soft_limit)


def test_fd_monitor_below_warn_no_action(monkeypatch):
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=100, soft_limit=65535)
    fd.check(svc)
    assert notifications.calls == []
    assert health.restart_calls == []


def test_fd_monitor_warn_threshold_fires_warning_once(monkeypatch):
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    # 70% of 65535 = 45874; use 50000 to clearly cross
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=50000, soft_limit=65535)
    fd.check(svc)
    fd.check(svc)  # second cycle at same level — should NOT re-fire
    warnings = [c for c in notifications.calls if c.get("event_type") == "fd_warning"]
    assert len(warnings) == 1
    assert health.restart_calls == []


def test_fd_monitor_critical_fires_alert_and_restart(monkeypatch):
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    # 90% of 65535 = 58981; use 60000
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=60000, soft_limit=65535)
    fd.check(svc)
    criticals = [c for c in notifications.calls if c.get("event_type") == "fd_critical"]
    assert len(criticals) == 1
    assert len(health.restart_calls) == 1
    assert "60000" in health.restart_calls[0][1]


def test_fd_monitor_recovery_after_warning(monkeypatch):
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    # First cycle: cross warn threshold
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=50000, soft_limit=65535)
    fd.check(svc)
    # Second cycle: drop below 50% (clear threshold = 32767)
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=20000, soft_limit=65535)
    fd.check(svc)
    recoveries = [c for c in notifications.calls if c.get("event_type") == "recovery"]
    assert len(recoveries) == 1


def test_fd_monitor_container_down_skips(monkeypatch):
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    import healthmonitor
    monkeypatch.setattr(healthmonitor, "get_container_pid", lambda name: None)
    fd.check(svc)
    assert notifications.calls == []
    assert health.restart_calls == []


def test_fd_monitor_implausibly_low_count_skips(monkeypatch):
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=2, soft_limit=65535)
    fd.check(svc)
    assert notifications.calls == []  # treated as race, ignored
    assert health.restart_calls == []


def test_fd_monitor_critical_does_not_re_fire_alert_on_subsequent_cycles(monkeypatch):
    """Critical alert fires once per excursion (hysteresis), but request_restart still runs each cycle."""
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    # 3 cycles all above critical
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=60000, soft_limit=65535)
    fd.check(svc)
    fd.check(svc)
    fd.check(svc)

    criticals = [c for c in notifications.calls if c.get("event_type") == "fd_critical"]
    assert len(criticals) == 1, f"expected 1 fd_critical alert, got {len(criticals)}"
    # request_restart still called every cycle — budget gate decides whether to retry
    assert len(health.restart_calls) == 3


def test_fd_monitor_logs_warning_when_restart_suppressed(monkeypatch, caplog):
    """When request_restart returns False (budget exhausted), FDMonitor logs a warning."""
    import logging
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()

    class _FakeHealthMonitorBlocked:
        def request_restart(self, svc, reason):
            return False  # budget exhausted

    health = _FakeHealthMonitorBlocked()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=60000, soft_limit=65535)
    with caplog.at_level(logging.WARNING, logger="healthmonitor"):
        fd.check(svc)

    suppressed = [r for r in caplog.records if "restart suppressed" in r.message]
    assert len(suppressed) == 1, f"expected 1 'restart suppressed' warning log, got {len(suppressed)}"


def test_health_monitor_constructs_fd_monitor():
    hm = _make_test_health_monitor()
    assert hasattr(hm, "fd_monitor")
    from healthmonitor import FDMonitor
    assert isinstance(hm.fd_monitor, FDMonitor)


def test_health_monitor_run_calls_fd_monitor_for_baserow_only(monkeypatch):
    """Each cycle, fd_monitor.check() is called exactly once and only for baserow."""
    hm = _make_test_health_monitor()

    # Replace fd_monitor.check with a recorder
    calls = []
    monkeypatch.setattr(hm.fd_monitor, "check", lambda svc: calls.append(svc.name))

    # Make process_service a no-op so we don't touch real services
    monkeypatch.setattr(hm, "process_service", lambda svc: None)
    monkeypatch.setattr(hm.notifications, "send_heartbeat", lambda: None)

    # Patch sleep + raise after one cycle to break the loop
    iteration = {"n": 0}
    def fake_sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 1:
            raise KeyboardInterrupt
    import healthmonitor
    monkeypatch.setattr(healthmonitor.time, "sleep", fake_sleep)

    try:
        hm.run()
    except KeyboardInterrupt:
        pass
    assert calls == ["baserow"]


def test_load_config_rejects_inverted_fd_thresholds(tmp_path, monkeypatch):
    """warn must be strictly less than critical."""
    import healthmonitor, pytest
    from healthmonitor import load_config
    (tmp_path / "config.env").write_text("FD_WARN_PERCENT=90\nFD_CRITICAL_PERCENT=70\n")
    monkeypatch.setattr(healthmonitor, "__file__", str(tmp_path / "healthmonitor.py"))
    with pytest.raises(ValueError, match="FD threshold"):
        load_config()


def test_load_config_rejects_zero_fd_warn(tmp_path, monkeypatch):
    """warn must be > 0."""
    import healthmonitor, pytest
    from healthmonitor import load_config
    (tmp_path / "config.env").write_text("FD_WARN_PERCENT=0\nFD_CRITICAL_PERCENT=90\n")
    monkeypatch.setattr(healthmonitor, "__file__", str(tmp_path / "healthmonitor.py"))
    with pytest.raises(ValueError, match="FD threshold"):
        load_config()


def test_load_config_rejects_critical_above_100(tmp_path, monkeypatch):
    """critical must be <= 100."""
    import healthmonitor, pytest
    from healthmonitor import load_config
    (tmp_path / "config.env").write_text("FD_WARN_PERCENT=70\nFD_CRITICAL_PERCENT=120\n")
    monkeypatch.setattr(healthmonitor, "__file__", str(tmp_path / "healthmonitor.py"))
    with pytest.raises(ValueError, match="FD threshold"):
        load_config()


def test_load_config_accepts_equal_critical_100(tmp_path, monkeypatch):
    """critical=100 is the upper bound and accepted."""
    import healthmonitor
    from healthmonitor import load_config
    (tmp_path / "config.env").write_text("FD_WARN_PERCENT=70\nFD_CRITICAL_PERCENT=100\n")
    monkeypatch.setattr(healthmonitor, "__file__", str(tmp_path / "healthmonitor.py"))
    cfg = load_config()
    assert cfg["fd_warn_percent"] == 70
    assert cfg["fd_critical_percent"] == 100


def test_fd_monitor_critical_re_fires_when_count_yo_yos_above_warn(monkeypatch):
    """Bug fix: critical hysteresis must clear when count drops below critical_threshold,
    not only when count drops below the recovery (50%) threshold. Otherwise a
    yo-yo between warn and critical bands silently suppresses re-alerts.
    """
    from healthmonitor import FDMonitor
    config = {"fd_warn_percent": 70, "fd_critical_percent": 90}
    notifications = _FakeNotifications()
    health = _FakeHealthMonitor()
    svc = _make_baserow_svc()
    fd = FDMonitor(config, notifications, health)

    # Thresholds at soft=65535: warn=45874, critical=58981, clear=32767

    # Cycle 1: cross critical (60000 > 58981) — fd_critical fires
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=60000, soft_limit=65535)
    fd.check(svc)

    # Cycle 2: drop to warn band (50000: between warn 45874 and critical 58981)
    # Count is BELOW critical but ABOVE clear. The bug suppressed re-fire after this.
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=50000, soft_limit=65535)
    fd.check(svc)

    # Cycle 3: climb back above critical — should re-fire
    _patch_fd_state(monkeypatch, root_pid=1000, fd_count=60000, soft_limit=65535)
    fd.check(svc)

    criticals = [c for c in notifications.calls if c.get("event_type") == "fd_critical"]
    assert len(criticals) == 2, f"expected 2 critical alerts (one per excursion), got {len(criticals)}"
