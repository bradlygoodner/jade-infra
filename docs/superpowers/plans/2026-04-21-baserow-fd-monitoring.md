# Baserow FD-Count Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a proactive file-descriptor probe to `healthmonitor.py` so baserow FD exhaustion is detected (and recovered) before users see HTTP 500s on the upload codepath.

**Architecture:** A new `FDMonitor` class inside `healthmonitor.py` samples baserow's process-tree FD count from host `/proc` each cycle, fires a Pushover/n8n warning at 70% of the soft `nofile` limit, and triggers a proactive container restart at 90% via a refactored `HealthMonitor.request_restart()` entry point that reuses the existing cooldown/budget/emergency machinery.

**Tech Stack:** Python 3 stdlib only (no new runtime deps); pytest for tests; existing systemd service (`propertyops-healthmonitor.service`); existing notification stack (n8n webhook → Pushover → Uptime Kuma).

**Source spec:** `docs/superpowers/specs/2026-04-21-baserow-fd-monitoring-design.md` (commit `5ecc22f`)

**Pre-execution decisions for the operator:**
1. **Branching.** This plan was not produced in a worktree. Recommend creating a branch before executing (`git checkout -b fd-monitoring`) so the work is reviewable separately from any other in-flight changes on master.
2. **pytest installation.** `pytest` is not currently installed on this host. Task 1 installs it via `apt`. If you'd rather use a venv, swap that step for `python3 -m venv ... && pip install pytest`.
3. **Healthmonitor restart timing.** Task 13 restarts `propertyops-healthmonitor.service` to pick up the new code. There is no service interruption to baserow itself (the monitor is a separate process), but the monitoring loop pauses briefly. Pick a quiet moment.

---

## File Structure

**Files modified:**
- `docker/scripts/healthmonitor.py` — add `FDMonitor` class + supporting helpers; refactor `HealthMonitor._should_restart` to share budget logic with new `request_restart` method; wire `FDMonitor.check()` into `HealthMonitor.run()` for baserow only.
- `docker/scripts/config.env` — add `FD_WARN_PERCENT` and `FD_CRITICAL_PERCENT` keys with defaults.

**Files created:**
- `docker/scripts/tests/__init__.py` — empty marker so pytest can discover the package.
- `docker/scripts/tests/conftest.py` — adds parent dir to `sys.path` so tests can `import healthmonitor`.
- `docker/scripts/tests/test_healthmonitor.py` — unit tests for new helpers, refactor, and FDMonitor state machine.

**Not modified:**
- `docker/baserow/docker-compose.yml` — Bucket 1 already raised the FD limit to 65535.
- `propertyops-healthmonitor.service` (systemd unit file) — no new dependencies, no env changes.
- n8n alert workflow — `event_type` is free-form; new event types route through the same handler. (Routing review tracked separately as a follow-up.)

---

## Task 1: Set up pytest harness

**Files:**
- Create: `docker/scripts/tests/__init__.py`
- Create: `docker/scripts/tests/conftest.py`
- Create: `docker/scripts/tests/test_healthmonitor.py` (smoke test only — real tests added in later tasks)

- [ ] **Step 1: Install pytest**

```bash
apt-get update && apt-get install -y python3-pytest
python3 -m pytest --version
```

Expected: `pytest 7.x.x` (or whatever Debian/Ubuntu currently ships).

- [ ] **Step 2: Create the tests package marker**

```bash
mkdir -p /root/docker/scripts/tests
touch /root/docker/scripts/tests/__init__.py
```

- [ ] **Step 3: Create conftest.py to make `healthmonitor` importable from tests**

Create `/root/docker/scripts/tests/conftest.py`:

```python
"""Shared pytest config for healthmonitor tests."""
import sys
from pathlib import Path

# Add docker/scripts/ to sys.path so tests can `import healthmonitor`
sys.path.insert(0, str(Path(__file__).parent.parent))
```

- [ ] **Step 4: Write a smoke test that just imports the module**

Create `/root/docker/scripts/tests/test_healthmonitor.py`:

```python
"""Tests for healthmonitor.py."""


def test_module_imports():
    """Smoke test: the module imports without error."""
    import healthmonitor
    assert healthmonitor.__name__ == "healthmonitor"
```

- [ ] **Step 5: Run the smoke test**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: `1 passed`. If it fails with an import error, conftest.py isn't on the path correctly — fix before continuing.

- [ ] **Step 6: Commit**

```bash
cd /root && git add docker/scripts/tests/ && git commit -m "test: scaffold pytest harness for healthmonitor"
```

---

## Task 2: Add `walk_process_tree` helper (TDD)

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (add new module-level function)
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write the failing test**

Append to `/root/docker/scripts/tests/test_healthmonitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 3 new tests fail with `ImportError: cannot import name 'walk_process_tree' from 'healthmonitor'`.

- [ ] **Step 3: Implement `walk_process_tree`**

Add to `/root/docker/scripts/healthmonitor.py` near the top (after the imports and logging setup, before the existing `load_config` function — module-level helpers grouped together):

```python
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
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        if not children_str:
            continue
        for child in children_str.split():
            try:
                queue.append(int(child))
            except ValueError:
                continue
    return pids
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 4 tests pass (smoke test + 3 walk tests).

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: add walk_process_tree helper for FD probe"
```

---

## Task 3: Add `count_open_fds` helper (TDD)

**Files:**
- Modify: `docker/scripts/healthmonitor.py`
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write the failing test**

Append to `test_healthmonitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 3 new tests fail with `ImportError: cannot import name 'count_open_fds'`.

- [ ] **Step 3: Implement `count_open_fds`**

Add to `healthmonitor.py` immediately after `walk_process_tree`:

```python
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
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return total
```

Also add the missing import at the top of the file (find the existing `import os` line — it's already there, no change needed; just verify).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: add count_open_fds helper for FD probe"
```

---

## Task 4: Add `read_soft_nofile_limit` helper (TDD)

**Files:**
- Modify: `docker/scripts/healthmonitor.py`
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write the failing test**

Append to `test_healthmonitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 3 new tests fail with `ImportError: cannot import name 'read_soft_nofile_limit'`.

- [ ] **Step 3: Implement `read_soft_nofile_limit`**

Add to `healthmonitor.py` immediately after `count_open_fds`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 10 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: add read_soft_nofile_limit helper for FD probe"
```

---

## Task 5: Add `get_container_pid` helper (TDD with subprocess mock)

**Files:**
- Modify: `docker/scripts/healthmonitor.py`
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write the failing test**

Append to `test_healthmonitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 4 new tests fail with `ImportError: cannot import name 'get_container_pid'`.

- [ ] **Step 3: Implement `get_container_pid`**

Add to `healthmonitor.py` immediately after `read_soft_nofile_limit`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 14 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: add get_container_pid helper for FD probe"
```

---

## Task 6: Refactor restart-budget logic out of `_should_restart` (TDD)

The current `HealthMonitor._should_restart` mixes two concerns:
- "Are we sure something is broken?" (consecutive_failures check)
- "Are we allowed to restart right now?" (cooldown / max_restarts / emergency)

The FD-critical path will need only the second check. Extract it into a separate method `_check_restart_budget` that both code paths can call.

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (lines ~334-363, the existing `_should_restart` method)
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write failing tests for `_check_restart_budget`**

Append to `test_healthmonitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 4 new tests fail. Some may fail with `AttributeError: '_check_restart_budget'`, the `_make_test_health_monitor` ones may pass partially before reaching the new method call — that's fine.

- [ ] **Step 3: Refactor `_should_restart` to extract `_check_restart_budget`**

In `healthmonitor.py`, replace the existing `_should_restart` method (around lines 334-363) with these two methods:

```python
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
```

- [ ] **Step 4: Run tests to verify all pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 18 tests pass. The existing `_should_restart` callers are unchanged in behavior because `_should_restart` still returns the same value for the same inputs.

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "refactor: extract _check_restart_budget from _should_restart"
```

---

## Task 7: Add `request_restart` method (TDD)

**Files:**
- Modify: `docker/scripts/healthmonitor.py`
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write the failing test**

Append to `test_healthmonitor.py`:

```python
def test_request_restart_blocked_by_budget(monkeypatch):
    """request_restart respects _check_restart_budget."""
    hm = _make_test_health_monitor()
    svc = hm.services[0]
    hm.states[svc.name].emergency = True

    called = []
    monkeypatch.setattr(hm, "_restart_service", lambda s: called.append(s) or True)

    result = hm.request_restart(svc, reason="test")
    assert result is False
    assert called == []  # restart not attempted


def test_request_restart_executes_when_allowed(monkeypatch):
    """request_restart calls _restart_service and updates state when budget allows."""
    hm = _make_test_health_monitor()
    svc = hm.services[0]

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
    svc = hm.services[0]

    monkeypatch.setattr(hm, "_restart_service", lambda s: False)
    monkeypatch.setattr(hm.notifications, "notify", lambda **kw: None)

    result = hm.request_restart(svc, reason="test")
    assert result is False
    state = hm.states[svc.name]
    assert state.restart_count == 1
    assert len(state.restart_times) == 1  # attempt counted toward budget even on failure
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 3 new tests fail with `AttributeError: 'HealthMonitor' object has no attribute 'request_restart'`.

- [ ] **Step 3: Implement `request_restart`**

Add this new method to the `HealthMonitor` class in `healthmonitor.py`, immediately after `_should_restart`:

```python
    def request_restart(self, svc: ServiceConfig, reason: str) -> bool:
        """Attempt to restart a service for any reason (FD critical, manual trigger, etc.).

        Reuses the same budget guard as the failure-driven path so callers can't
        bypass max_restarts/cooldown/emergency. Returns True if the restart command
        was issued AND succeeded, False otherwise.

        The `reason` string is included in the restart_initiated notification so
        operators see *why* a restart fired (vs. "n consecutive failures").
        """
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 21 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: add HealthMonitor.request_restart for non-failure-driven restart triggers"
```

---

## Task 8: Migrate the existing failure-driven restart in `process_service` to use `request_restart`

The existing inline restart logic in `process_service` (lines ~466-496) duplicates everything that's now in `request_restart`. Replace it with a one-line call so future changes to restart behavior only need to happen in one place.

**Files:**
- Modify: `docker/scripts/healthmonitor.py`
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write a regression test asserting the existing failure-driven path still works**

Append to `test_healthmonitor.py`:

```python
def test_process_service_failure_driven_restart_uses_request_restart(monkeypatch):
    """When consecutive_failures crosses threshold, process_service calls request_restart with a useful reason."""
    hm = _make_test_health_monitor()
    svc = hm.services[0]

    # Force the health check to report service_issue
    monkeypatch.setattr(hm, "check_service", lambda s: "service_issue")

    # Capture request_restart calls
    calls = []
    monkeypatch.setattr(hm, "request_restart", lambda s, reason: calls.append((s.name, reason)) or True)

    # First failure — below threshold, no restart
    hm.process_service(svc)
    assert calls == []
    assert hm.states[svc.name].consecutive_failures == 1

    # Second failure — threshold met, restart requested
    hm.process_service(svc)
    assert len(calls) == 1
    name, reason = calls[0]
    assert name == "baserow"
    assert "consecutive" in reason.lower() or "failures" in reason.lower()
```

- [ ] **Step 2: Run tests to verify the new one fails**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: the new test fails because `process_service` currently does its restart inline, not via `request_restart`.

- [ ] **Step 3: Update `process_service` to delegate to `request_restart`**

In `healthmonitor.py`, find the existing `process_service` method's "service_issue" branch (around line 462 onwards). Replace the block starting at `state.record_failure()` and ending at the closing of the `if self._should_restart(svc):` block with this:

```python
        state.record_failure()
        logger.warning("FAILURE: %s — consecutive failures: %d",
                       svc.name, state.consecutive_failures)

        if self._should_restart(svc):
            self.request_restart(
                svc,
                reason=f"{state.consecutive_failures} consecutive health check failures",
            )
```

(Delete the old inline notification + `_restart_service` + state update code that was here. All of that now lives in `request_restart`.)

- [ ] **Step 4: Run all tests**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 22 tests pass. No prior test should regress.

- [ ] **Step 5: Quick visual diff review**

```bash
cd /root && git diff docker/scripts/healthmonitor.py
```

Confirm: the only change is replacing the inline restart block with a one-line `self.request_restart(svc, reason=...)`. No other behavior changes.

- [ ] **Step 6: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "refactor: route process_service failure restart through request_restart"
```

---

## Task 9: Add `FD_WARN_PERCENT` and `FD_CRITICAL_PERCENT` to config (TDD)

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (the `load_config` function)
- Modify: `docker/scripts/tests/test_healthmonitor.py`
- Modify: `docker/scripts/config.env` (the live config file — done in Task 13 deploy step, NOT here)

- [ ] **Step 1: Write the failing test**

Append to `test_healthmonitor.py`:

```python
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
    assert cfg["fd_warn_percent"] == 70
    assert cfg["fd_critical_percent"] == 90


def test_load_config_fd_custom_values(tmp_path, monkeypatch):
    """load_config reads custom FD thresholds from config.env."""
    import healthmonitor
    from healthmonitor import load_config
    (tmp_path / "config.env").write_text("FD_WARN_PERCENT=60\nFD_CRITICAL_PERCENT=85\n")
    monkeypatch.setattr(healthmonitor, "__file__", str(tmp_path / "healthmonitor.py"))
    cfg = load_config()
    assert cfg["fd_warn_percent"] == 60
    assert cfg["fd_critical_percent"] == 85
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 2 new tests fail with `KeyError: 'fd_warn_percent'`.

- [ ] **Step 3: Update `load_config` to include FD threshold defaults**

In `healthmonitor.py`, find the `load_config` function and update the returned dict to include the two new keys:

```python
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
        "fd_warn_percent": int(raw.get("FD_WARN_PERCENT", "70")),
        "fd_critical_percent": int(raw.get("FD_CRITICAL_PERCENT", "90")),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 24 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: add FD_WARN_PERCENT and FD_CRITICAL_PERCENT config keys"
```

---

## Task 10: Add `fd_warning` and `fd_critical` to `PUSHOVER_PRIORITY_MAP`

This is a one-line constant change. No TDD ceremony — verify by reading.

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (the `PUSHOVER_PRIORITY_MAP` dict near line 79)

- [ ] **Step 1: Update the priority map**

In `healthmonitor.py`, find `PUSHOVER_PRIORITY_MAP` and add the two new keys:

```python
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
```

- [ ] **Step 2: Run all tests to confirm no regression**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 24 tests still pass.

- [ ] **Step 3: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py && git commit -m "feat: add fd_warning and fd_critical to Pushover priority map"
```

---

## Task 11: Add `FDMonitor` class with state machine (TDD)

**Files:**
- Modify: `docker/scripts/healthmonitor.py`
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write failing tests for `FDMonitor`**

Append to `test_healthmonitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 6 new tests fail with `ImportError: cannot import name 'FDMonitor'`.

- [ ] **Step 3: Implement `FDMonitor`**

Add to `healthmonitor.py` immediately after the `HealthMonitor` class (around line 567):

```python
# ── FD Monitor ────────────────────────────────────────────────────────────────

@dataclass
class FDMonitorState:
    """Per-service mutable state for FDMonitor."""
    last_warning_fired: bool = False
    last_alert_count: int = 0


class FDMonitor:
    """Probes baserow's process-tree FD count and triggers warnings/restarts.

    Uses the soft `nofile` rlimit as the denominator for percent calculations.
    Reuses HealthMonitor.request_restart() for the critical path so this
    code path inherits cooldown/budget/emergency semantics.
    """

    HYSTERESIS_CLEAR_PERCENT = 50
    IMPLAUSIBLY_LOW_FD_COUNT = 10

    def __init__(self, config: dict, notifications: "NotificationManager", health_monitor: "HealthMonitor"):
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

        if count < self.IMPLAUSIBLY_LOW_FD_COUNT:
            logger.debug("FDMonitor: %s FD count %d implausibly low, skipping", svc.name, count)
            return

        warn_threshold = soft_limit * self.config["fd_warn_percent"] // 100
        critical_threshold = soft_limit * self.config["fd_critical_percent"] // 100
        clear_threshold = soft_limit * self.HYSTERESIS_CLEAR_PERCENT // 100

        pct = count * 100 // soft_limit
        logger.debug("FDMonitor: %s FDs=%d soft=%d (%d%%)", svc.name, count, soft_limit, pct)

        if count >= critical_threshold:
            self._fire_critical(svc, count, soft_limit, pct)
            state.last_alert_count = max(state.last_alert_count, count)
            state.last_warning_fired = True  # track so recovery fires when count drops
            self.health_monitor.request_restart(
                svc, reason=f"FD count {count}/{soft_limit} ({pct}%)"
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 30 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: add FDMonitor with warn/critical/recovery state machine"
```

---

## Task 12: Wire `FDMonitor` into `HealthMonitor.run()`

**Files:**
- Modify: `docker/scripts/healthmonitor.py` (the `HealthMonitor.__init__` and `run` methods)
- Modify: `docker/scripts/tests/test_healthmonitor.py`

- [ ] **Step 1: Write the failing test**

Append to `test_healthmonitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: 2 new tests fail — first with `AttributeError: 'HealthMonitor' object has no attribute 'fd_monitor'`.

- [ ] **Step 3: Construct `FDMonitor` in `HealthMonitor.__init__`**

In `healthmonitor.py`, update `HealthMonitor.__init__` to instantiate `FDMonitor`:

```python
    def __init__(self, config: dict, notifications: NotificationManager):
        self.config = config
        self.notifications = notifications
        self.services = get_service_configs(config["compose_base"])
        self.states: dict[str, ServiceState] = {
            svc.name: ServiceState() for svc in self.services
        }
        self.fd_monitor = FDMonitor(config, notifications, self)
```

(The `FDMonitor` class is defined later in the file, but Python resolves the name at call time, not class-definition time, so this works as long as `FDMonitor` is in scope when `HealthMonitor` is instantiated.)

- [ ] **Step 4: Wire `fd_monitor.check()` into the main loop**

In `HealthMonitor.run`, modify the per-cycle loop body. The existing code is:

```python
            for svc in self.services:
                try:
                    self.process_service(svc)
                except Exception as e:
                    logger.error("Unexpected error processing %s: %s", svc.name, e)
```

Replace with:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 32 tests pass.

- [ ] **Step 6: Commit**

```bash
cd /root && git add docker/scripts/healthmonitor.py docker/scripts/tests/test_healthmonitor.py && git commit -m "feat: wire FDMonitor into HealthMonitor.run for baserow"
```

---

## Task 13: Deploy to live host and end-to-end verify

This is a manual deploy + verification task. No code changes — but it MUST happen for the work to actually take effect.

**Files:**
- Modify: `docker/scripts/config.env` (live config — add the two new keys)

- [ ] **Step 1: Add the new keys to the live `config.env`**

Append to `/root/docker/scripts/config.env` (preserve existing content; add at the bottom):

```
# FD monitoring (baserow only in v1) — see docs/superpowers/specs/2026-04-21-baserow-fd-monitoring-design.md
FD_WARN_PERCENT=70
FD_CRITICAL_PERCENT=90
```

- [ ] **Step 2: Verify the systemd unit will pick up the new code**

Check that the unit runs the script directly (not a stale frozen copy):

```bash
systemctl cat propertyops-healthmonitor.service | grep ExecStart
```

Expected: `ExecStart=` points at `/root/docker/scripts/healthmonitor.py` (or equivalent — the file we've been editing).

- [ ] **Step 3: Restart the service**

```bash
systemctl restart propertyops-healthmonitor.service
until systemctl is-active propertyops-healthmonitor.service >/dev/null 2>&1; do sleep 2; done
systemctl status propertyops-healthmonitor.service --no-pager
```

Expected: `Active: active (running)`. If it's failed, check `journalctl -u propertyops-healthmonitor.service -n 50` and resolve before continuing — most likely a syntax error or missing import.

- [ ] **Step 4: Confirm the FD probe runs without error**

```bash
journalctl -u propertyops-healthmonitor.service -n 100 --no-pager | grep -iE 'fd|baserow'
```

Expected: see `FDMonitor:` debug/info lines for baserow. No errors. If you don't see any FDMonitor lines, the loop isn't reaching it — investigate before proceeding.

- [ ] **Step 5: End-to-end alert test (warning path)**

Temporarily lower the warning threshold so it fires immediately:

```bash
sed -i 's/^FD_WARN_PERCENT=.*/FD_WARN_PERCENT=1/' /root/docker/scripts/config.env
SINCE=$(date '+%Y-%m-%d %H:%M:%S')
systemctl restart propertyops-healthmonitor.service
# Wait until the probe has run at least once (look for fd_warning or 'warning threshold' in the journal since restart)
until journalctl -u propertyops-healthmonitor.service --since "$SINCE" --no-pager | grep -qiE 'fd_warning|warning threshold'; do sleep 2; done
journalctl -u propertyops-healthmonitor.service --since "$SINCE" --no-pager | grep -i 'fd_warning\|warning threshold'
```

Expected: you see a "warning threshold (1%) crossed" log line, and you receive a Pushover (or n8n) notification with `event_type: fd_warning`. (If the loop hangs >2 minutes, the probe isn't running — check `journalctl -u propertyops-healthmonitor.service` for errors and abort.)

- [ ] **Step 6: Restore the warning threshold and verify recovery fires**

```bash
sed -i 's/^FD_WARN_PERCENT=.*/FD_WARN_PERCENT=70/' /root/docker/scripts/config.env
SINCE=$(date '+%Y-%m-%d %H:%M:%S')
systemctl restart propertyops-healthmonitor.service
until systemctl is-active propertyops-healthmonitor.service >/dev/null 2>&1; do sleep 2; done
journalctl -u propertyops-healthmonitor.service --since "$SINCE" --no-pager | head -40
```

Expected: probe is back to normal (debug-level FD lines for baserow, no warning).

(Note: a service restart resets `FDMonitor.states`, so the **recovery notification** does NOT fire here — the recovery code path requires `last_warning_fired=True` to be set in the same long-running session. This is expected behavior, not a bug. The recovery path is fully covered by unit tests in Task 11; real-world recovery fires when the probe is the same long-lived process that originally fired the warning.)

- [ ] **Step 7: Confirm normal operation restored**

```bash
systemctl status propertyops-healthmonitor.service --no-pager | head -20
```

Expected: `Active: active (running)`, no recent failures, normal monitoring cadence in the logs.

- [ ] **Step 8: Commit the live config change**

```bash
cd /root && git add docker/scripts/config.env && git commit -m "feat: enable baserow FD monitoring in live config"
```

(Note: `config.env` was already in your pending working-tree changes from before this plan — review the diff carefully and decide whether to commit just the FD additions or bundle the rest. A clean diff via `git add -p` is recommended.)

---

## Task 14: Update CLAUDE-facing memory and close out

- [ ] **Step 1: Update auto-memory `project_baserow_fd_leak.md`**

Edit `/root/.claude/projects/-root/memory/project_baserow_fd_leak.md` to reflect that proactive FD monitoring is now in place:

Replace the "How to apply" section with:

```markdown
**How to apply:** When the user reports recurring "database" or "upload" issues on the plants Baserow stack, first check whether healthmonitor's FD probe has been firing — `journalctl -u propertyops-healthmonitor.service --since "24h ago" | grep -i 'fd_warning\|fd_critical'`. If silent, double-check the probe is running (`systemctl status propertyops-healthmonitor.service`). If firing, look at peak counts to decide whether to bump the FD limit further (compose `ulimits:` block), tune thresholds, or escalate to upstream baserow as a real leak.
```

- [ ] **Step 2: Verify all tests still pass one final time**

```bash
cd /root/docker/scripts && python3 -m pytest tests/ -v
```

Expected: all 32 tests pass.

- [ ] **Step 3: Final summary commit (memory update)**

The memory file lives outside the git repo, so no commit needed for that. The implementation work is now complete.

- [ ] **Step 4: Push or PR (operator decision)**

If you branched per the pre-execution recommendation, push the branch and open a PR. If you committed directly to master, push when ready:

```bash
cd /root && git log --oneline origin/master..HEAD
```

Review the commit list, then push.

---

## Verification summary

After all tasks complete, the following are true:
- ✅ `python3 -m pytest /root/docker/scripts/tests/ -v` reports 32 tests pass
- ✅ `systemctl status propertyops-healthmonitor.service` shows active running
- ✅ `journalctl -u propertyops-healthmonitor.service` shows `FDMonitor:` debug lines for baserow each cycle
- ✅ Test threshold reduction triggers a real Pushover/n8n notification with `event_type: fd_warning`
- ✅ Restoring the threshold restores normal operation
- ✅ Bucket 2 of the optimization plan is shipped; Bucket 5 (centralized logs) and Bucket 1 (already shipped) cover the broader investigation
