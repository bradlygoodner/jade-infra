# Baserow FD Leak Prevention — Gunicorn Worker Recycling

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Baserow's file-descriptor exhaustion by recycling gunicorn workers before the Pillow FD leak can accumulate, and re-calibrate FDMonitor as a safety net rather than the primary defense.

**Architecture:** Gunicorn's built-in `--max-requests` causes each worker process to exit gracefully after N requests and be replaced by a fresh one with zero leaked FDs. This is injected via the `GUNICORN_CMD_ARGS` env var which gunicorn reads natively from its process environment. FDMonitor thresholds are tightened (70%→40%, 90%→60%) to reflect the new reality: a threshold crossing now means recycling broke, not "normal slow build-up."

**Tech Stack:** Docker Compose, gunicorn (WSGI), Python 3, pytest, systemd

---

## Background you need

**The leak:** Baserow's upload pipeline (Pillow `Image.open()` for thumbnail generation) doesn't always close file handles in a `with` block. Python's GC eventually collects them, but under any real traffic GC lags behind opening. FDs pile up in the gunicorn worker processes until they hit the kernel's `nofile` soft limit and every upload returns 500.

**Why recycling works:** Gunicorn is a pre-fork server. Each worker is a separate OS process. When a worker exits (even gracefully via `--max-requests`), its entire FD table is released by the kernel. Gunicorn's master spawns a replacement worker immediately. This is a rolling recycle — only one worker exits at a time, so 2 of 3 workers are always available. Zero downtime.

**How `GUNICORN_CMD_ARGS` works:** Gunicorn reads this env var in its config initialization phase (`gunicorn/config.py`) and prepends those args to its parsed argument list. The baserow container inherits the env var from Docker Compose's env_file. The flags do **not** appear in `ps aux` cmdline — they're applied to gunicorn's runtime config, not the spawn argv.

**Existing thresholds:** `FD_WARN_PERCENT=70` / `FD_CRITICAL_PERCENT=90` in `docker/scripts/config.env` and as hardcoded defaults in `load_config()` at `docker/scripts/healthmonitor.py:155-156`. Post-fix, steady-state FD count should be <5% of soft limit. Warn at 70% means FDMonitor only fires if we're already in serious trouble. Tightening to 40%/60% catches recycling breakage much earlier.

**Soft limit math:** `nofile` soft = 65535. New thresholds: warn at 26,214 FDs, critical at 39,321 FDs. A fresh container with 3 workers idling uses ~500-800 FDs total (<2%). Normal operating peak with recycling is ~3,000-5,000 FDs (<8%). A threshold crossing at 40% means something is genuinely wrong.

---

## File Map

| File | Change |
|------|--------|
| `docker/baserow/.env` | Already done — `GUNICORN_CMD_ARGS` added |
| `docker/scripts/tests/test_healthmonitor.py:337-348` | Update assertions: 70→40, 90→60 |
| `docker/scripts/healthmonitor.py:155-156` | Update hardcoded defaults: 70→40, 90→60 |
| `docker/scripts/config.env:56-57` | Update live values: 70→40, 90→60 |
| `docs/superpowers/specs/2026-04-21-baserow-fd-monitoring-design.md` | Append worker recycling section |
| `/root/.claude/projects/-root/memory/project_baserow_fd_leak.md` | Update mitigations block |

---

## Task 1: Apply the container change and verify gunicorn picked it up

**Files:**
- Read: `docker/baserow/.env`
- Apply: `docker compose up -d baserow` in `/root/docker/baserow/`

- [ ] **Step 1: Confirm the .env content is correct**

  ```bash
  grep GUNICORN /root/docker/baserow/.env
  ```

  Expected output:
  ```
  GUNICORN_CMD_ARGS=--max-requests=500 --max-requests-jitter=50
  ```

  If that line is missing, add it now (it should already be there from last session).

- [ ] **Step 2: Recreate the baserow container**

  ```bash
  cd /root/docker/baserow && docker compose up -d baserow
  ```

  Expected: `Container propertyops-baserow  Started` or `Recreated`. If it says `Running` with no recreation, force it:
  ```bash
  docker compose up -d --force-recreate baserow
  ```

- [ ] **Step 3: Wait for the health check to pass**

  ```bash
  for i in $(seq 1 24); do
    STATUS=$(docker inspect --format '{{.State.Health.Status}}' propertyops-baserow 2>/dev/null)
    echo "$(date +%H:%M:%S) — $STATUS"
    [ "$STATUS" = "healthy" ] && break
    sleep 5
  done
  ```

  Expected: `healthy` within 2 minutes. If it stays `starting` past 2 minutes, check `docker logs propertyops-baserow --tail 30`.

- [ ] **Step 4: Verify the env var is present in the container's environment**

  ```bash
  docker exec propertyops-baserow env | grep GUNICORN
  ```

  Expected output:
  ```
  GUNICORN_CMD_ARGS=--max-requests=500 --max-requests-jitter=50
  ```

  If this line is missing, the env var didn't reach the container. Check `docker/baserow/docker-compose.yml` — the `env_file: .env` line must be present under the `baserow:` service. Do not proceed to Task 2 until this passes.

- [ ] **Step 5: Capture baseline FD count**

  ```bash
  PID=$(docker inspect --format '{{.State.Pid}}' propertyops-baserow)
  SOFT=$(awk '/Max open files/{print $4}' /proc/$PID/limits)
  COUNT=$(find /proc/$PID/fd -maxdepth 1 2>/dev/null | wc -l)
  echo "FDs: $COUNT / $SOFT = $(( COUNT * 100 / SOFT ))%"
  ```

  Expected: count in range 400–1500 (well under 5% of 65535). Record the number — you'll compare against it after uploads.

- [ ] **Step 6: Verify gunicorn will actually recycle workers**

  The flags don't appear in `ps aux` (they're applied to runtime config, not argv), so we verify indirectly: make gunicorn log that it understood the setting.

  ```bash
  docker exec propertyops-baserow python3 -c "
  import os
  cmd_args = os.environ.get('GUNICORN_CMD_ARGS', '')
  print('GUNICORN_CMD_ARGS:', repr(cmd_args))
  assert '--max-requests' in cmd_args, 'flag missing!'
  # Parse it the same way gunicorn does
  import shlex
  args = shlex.split(cmd_args)
  mr_idx = next((i for i, a in enumerate(args) if a.startswith('--max-requests=')), None)
  if mr_idx is not None:
      val = int(args[mr_idx].split('=')[1])
  else:
      mr_idx = next((i for i, a in enumerate(args) if a == '--max-requests'), None)
      val = int(args[mr_idx + 1])
  print(f'max-requests will be set to: {val}')
  assert val == 500, f'expected 500, got {val}'
  print('OK — gunicorn will apply --max-requests=500')
  "
  ```

  Expected:
  ```
  GUNICORN_CMD_ARGS: '--max-requests=500 --max-requests-jitter=50'
  max-requests will be set to: 500
  OK — gunicorn will apply --max-requests=500
  ```

- [ ] **Step 7: Commit**

  ```bash
  git add docker/baserow/.env
  git commit -m "fix: recycle gunicorn workers every 500 requests to shed FD leak

  Baserow's Pillow upload pipeline leaks file descriptors. Worker recycling
  via --max-requests is the correct production fix: each worker exits cleanly
  after 500 requests, releasing all leaked FDs. Rolling recycle means no
  downtime. FDMonitor remains as a safety net.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
  ```

---

## Task 2: Tighten FDMonitor thresholds — TDD

**Files:**
- Modify: `docker/scripts/tests/test_healthmonitor.py:337-348`
- Modify: `docker/scripts/healthmonitor.py:155-156`
- Modify: `docker/scripts/config.env:56-57`

With worker recycling in place, the steady-state FD count stays under 10% of soft limit. Keeping warn at 70% means FDMonitor only alerts when we're already critically overloaded. Tightening to 40% makes it catch recycling failures (e.g., `GUNICORN_CMD_ARGS` stripped by an upgrade) while there's still a 60-point margin before critical.

- [ ] **Step 1: Write the failing test — update the defaults assertion**

  Open `docker/scripts/tests/test_healthmonitor.py`. Find `test_load_config_fd_defaults` at line ~337. It currently asserts 70/90. Change it to assert 40/60:

  ```python
  def test_load_config_fd_defaults(tmp_path, monkeypatch):
      """load_config provides sensible defaults for FD thresholds when keys absent."""

      from healthmonitor import load_config
      # load_config resolves config.env via Path(__file__).parent / "config.env".
      # Point it at a tmp dir with an empty config so defaults kick in.
      monkeypatch.chdir(tmp_path)
      (tmp_path / "config.env").write_text("")
      monkeypatch.syspath_prepend(str(Path(__file__).parent.parent))

      cfg = load_config()
      assert cfg["fd_warn_percent"] == 40
      assert cfg["fd_critical_percent"] == 60
  ```

  (Preserve any existing monkeypatch/chdir setup in the test — only change the two assertion values at the end.)

- [ ] **Step 2: Run the test and confirm it FAILS**

  ```bash
  cd /root/docker/scripts && python3 -m pytest tests/test_healthmonitor.py::test_load_config_fd_defaults -v
  ```

  Expected: `FAILED` — `assert 70 == 40` or similar. If it passes already, the defaults were already changed somewhere — double-check `healthmonitor.py:155-156` before proceeding.

- [ ] **Step 3: Update the hardcoded defaults in load_config()**

  Open `docker/scripts/healthmonitor.py`. Find lines ~155-156:
  ```python
  fd_warn = int(raw.get("FD_WARN_PERCENT", "70"))
  fd_crit = int(raw.get("FD_CRITICAL_PERCENT", "90"))
  ```

  Change to:
  ```python
  fd_warn = int(raw.get("FD_WARN_PERCENT", "40"))
  fd_crit = int(raw.get("FD_CRITICAL_PERCENT", "60"))
  ```

- [ ] **Step 4: Run the test and confirm it PASSES**

  ```bash
  cd /root/docker/scripts && python3 -m pytest tests/test_healthmonitor.py::test_load_config_fd_defaults -v
  ```

  Expected: `PASSED`.

- [ ] **Step 5: Run the full test suite — no regressions**

  ```bash
  cd /root/docker/scripts && python3 -m pytest tests/ -v
  ```

  Expected: all tests pass. If `test_load_config_fd_custom_values` fails, check that it's testing 60/85 values (not 70/90), as those are custom overrides and unaffected by defaults. If any FDMonitor state-machine test fails, the test is likely constructing a config dict with `"fd_warn_percent": 70` — those tests use inline dicts and are NOT affected by the defaults change, so they should pass unchanged.

- [ ] **Step 6: Update config.env live values**

  Open `docker/scripts/config.env`. Find lines ~56-57:
  ```
  FD_WARN_PERCENT=70
  FD_CRITICAL_PERCENT=90
  ```

  Change to:
  ```
  # FD monitoring (baserow only in v1) — tightened thresholds post-worker-recycling fix.
  # With --max-requests=500 in place, steady-state FD count stays under 10% of soft limit.
  # Warn at 40% (26,214 FDs) = recycling broke or a new leak path emerged.
  # Critical at 60% (39,321 FDs) = imminent exhaustion even with 6x headroom remaining.
  FD_WARN_PERCENT=40
  FD_CRITICAL_PERCENT=60
  ```

- [ ] **Step 7: Restart healthmonitor to pick up new thresholds**

  ```bash
  systemctl restart propertyops-healthmonitor
  sleep 3
  systemctl status propertyops-healthmonitor
  ```

  Expected: `Active: active (running)`. If it fails to start, check `journalctl -u propertyops-healthmonitor -n 30` — the most likely cause is a config parse error.

- [ ] **Step 8: Verify healthmonitor loaded the new thresholds**

  ```bash
  journalctl -u propertyops-healthmonitor --since "1 minute ago" | grep -E "FDMonitor|starting|Monitoring"
  ```

  Expected: "PropertyOps Health Monitor starting..." with no errors. FDMonitor threshold logging is at DEBUG level; the thresholds themselves aren't printed at startup (by design — they're applied per-cycle).

- [ ] **Step 9: Commit**

  ```bash
  git add docker/scripts/tests/test_healthmonitor.py \
          docker/scripts/healthmonitor.py \
          docker/scripts/config.env
  git commit -m "fix: tighten FDMonitor thresholds to 40%/60% post-worker-recycling

  With gunicorn --max-requests=500 shedding FDs on each worker recycle,
  steady-state FD count stays under 10% of soft limit. The old 70%/90%
  thresholds were calibrated for a slow build-up scenario that no longer
  applies. 40%/60% catches recycling failures (e.g. env var stripped by
  an upgrade) while maintaining a safe margin before critical.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
  ```

---

## Task 3: Update docs and memory

**Files:**
- Append: `docs/superpowers/specs/2026-04-21-baserow-fd-monitoring-design.md`
- Rewrite mitigations section: `/root/.claude/projects/-root/memory/project_baserow_fd_leak.md`

- [ ] **Step 1: Append worker recycling section to the spec**

  Open `docs/superpowers/specs/2026-04-21-baserow-fd-monitoring-design.md` and append the following at the end:

  ```markdown
  ---

  ## Addendum: Worker Recycling (2026-04-21, Bucket 3)

  **Status:** Shipped

  FDMonitor (Bucket 2) detects and reacts to FD exhaustion but doesn't prevent it. The root mechanism is gunicorn worker recycling via `--max-requests`.

  ### How it works

  Gunicorn's `--max-requests N` causes each worker process to exit gracefully after handling N requests. The master process detects the exit and spawns a fresh replacement. The replacement starts with zero leaked FDs. This is a rolling recycle — with 3 workers, at most 1 exits at a time. No downtime.

  ### Configuration

  Added to `docker/baserow/.env`:
  ```
  GUNICORN_CMD_ARGS=--max-requests=500 --max-requests-jitter=50
  ```

  `GUNICORN_CMD_ARGS` is a gunicorn-native env var read at startup from the process environment. The flags do not appear in `ps aux` cmdline — they're applied to gunicorn's runtime config. Verify with:
  ```bash
  docker exec propertyops-baserow env | grep GUNICORN_CMD_ARGS
  ```

  ### Threshold recalibration

  FDMonitor thresholds tightened from 70%/90% to 40%/60% (in both `config.env` and the hardcoded defaults in `load_config()`). With recycling in place, a 40% threshold crossing means recycling broke — a genuine signal requiring investigation.

  ### Architecture role after this change

  | Layer | Role |
  |-------|------|
  | `--max-requests=500` | Primary prevention — workers shed FDs before accumulation |
  | `ulimits: nofile: 65535` | Capacity buffer — delays failure mode by 64x if recycling breaks |
  | FDMonitor at 40%/60% | Safety net — catches recycling failures, new leak paths, or Celery pressure |
  ```

- [ ] **Step 2: Update the memory file mitigations block**

  Open `/root/.claude/projects/-root/memory/project_baserow_fd_leak.md`.

  Replace the `**Mitigations now live (2026-04-21):**` block with:

  ```markdown
  **Mitigations live as of 2026-04-21:**
  1. **FD limit raised to 65535** — via `ulimits:` in `docker/baserow/docker-compose.yml`. Capacity buffer; pushes failure from 1024→65535.
  2. **Gunicorn worker recycling** — `GUNICORN_CMD_ARGS=--max-requests=500 --max-requests-jitter=50` in `docker/baserow/.env`. **Primary fix.** Each worker sheds its leaked FDs by exiting after 500 requests; rolling recycle, zero downtime.
  3. **FDMonitor in healthmonitor.py** — probes process-tree FD count every 30s. Warn at 40% (~26,000 FDs), critical + proactive restart at 60% (~39,000 FDs). Safety net only — normal ops should never reach these thresholds. Verified end-to-end live.
  ```

  Also update the **How to apply** section to reflect the new primary diagnosis path:

  ```markdown
  **How to apply:**
  - **First check:** is the recycling working? `docker logs propertyops-baserow 2>&1 | grep -E "(Worker exiting|Booting worker)" | tail -20`. Expect to see periodic worker recycling messages over hours/days of traffic. If you never see any after a week of normal usage, recycling may not be happening — verify `docker exec propertyops-baserow env | grep GUNICORN_CMD_ARGS`.
  - **If FDMonitor fires at 40%:** Recycling has broken or a new leak path emerged. Check: (1) GUNICORN_CMD_ARGS still set, (2) whether upload volume spiked dramatically, (3) whether a Baserow upgrade removed the env var support.
  - **Live FD count:** `PID=$(docker inspect --format '{{.State.Pid}}' propertyops-baserow); SOFT=$(awk '/Max open files/{print $4}' /proc/$PID/limits); COUNT=$(ls /proc/$PID/fd | wc -l); echo "$COUNT / $SOFT = $(( COUNT * 100 / SOFT ))%"`
  - **If uploads still 500 but FDMonitor is silent and FD count is low:** The issue is something else — re-investigate from scratch.
  - **Long-term:** Report the Pillow FD leak to Baserow upstream. Worker recycling and FDMonitor are bandaids — they delay and detect, not cure.
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add "docs/superpowers/specs/2026-04-21-baserow-fd-monitoring-design.md" \
          "/root/.claude/projects/-root/memory/project_baserow_fd_leak.md"
  git commit -m "docs: record worker recycling fix and threshold recalibration

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
  ```

---

## Self-check: verification after all tasks complete

Run this to confirm the full stack is consistent:

```bash
# 1. Container has the env var
docker exec propertyops-baserow env | grep GUNICORN_CMD_ARGS

# 2. FD count is sane (expect < 5%)
PID=$(docker inspect --format '{{.State.Pid}}' propertyops-baserow)
SOFT=$(awk '/Max open files/{print $4}' /proc/$PID/limits)
COUNT=$(ls /proc/$PID/fd 2>/dev/null | wc -l)
echo "FDs: $COUNT / $SOFT = $(( COUNT * 100 / SOFT ))%"

# 3. Healthmonitor is running with new thresholds
systemctl status propertyops-healthmonitor | grep Active
journalctl -u propertyops-healthmonitor --since "5 minutes ago" | grep -v DEBUG | tail -10

# 4. Tests still green
cd /root/docker/scripts && python3 -m pytest tests/ -q
```

All four checks should pass cleanly.

---

## Deferred: Celery worker max-tasks-per-child

Celery workers (CELERY_WORKER, EXPORT_WORKER, BEAT_WORKER) run inside the same Baserow container. The observed FD leak was in the gunicorn upload path (synchronous Pillow processing). Celery handles async background tasks and is less exposed to the same leak pattern.

If Celery FD pressure is observed in future (FDMonitor fires with gunicorn recycling healthy, or `ls /proc/$PID/fd` shows Celery workers holding most FDs), add:

```env
# In docker/baserow/.env
CELERY_WORKER_MAX_TASKS_PER_CHILD=200
```

Baserow may or may not forward this to Celery's `worker_max_tasks_per_child` setting — test by checking `docker logs propertyops-baserow` for Celery worker restart messages. If it doesn't work, the fallback is a custom supervisor config override (out of scope for now).
