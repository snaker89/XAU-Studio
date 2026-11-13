"""Resumable XAU queue. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import email.utils
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DONE = {"Verified", "Unsupported"}


def remaining_text(seconds):
    seconds = max(0, math.ceil(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (f'{hours} h ' if hours else '') + f'{minutes} min {seconds:02d} sec'


def format_runtime_status(status, counts, now, cooldown=0):
    mode, until = status['mode'], status.get('until', 0)
    detail = status.get('detail', '')
    if cooldown > now and mode not in ('completed', 'paused', 'stopped'):
        mode, until = 'rate_limit', cooldown
    labels = {'processing': 'PROCESSING', 'rate_limit': 'RATE LIMIT',
              'session': 'SIGN-IN / SESSION', 'restart_budget': 'RESTART BUDGET EXHAUSTED',
              'restart_spacing': 'WAITING BEFORE RESTART', 'restart_disabled': 'RESTARTS DISABLED',
              'recovery': 'RECOVERY', 'waiting': 'WAITING TO RETRY',
              'completed': 'COMPLETED', 'paused': 'PAUSED', 'stopped': 'STOPPED'}
    parts = [labels.get(mode, mode.upper())]
    if until > now:
        action = 'Restart available in' if mode in ('restart_budget', 'restart_spacing') else 'Resuming in'
        parts.append(f'{action} {remaining_text(until - now)}')
    elif mode in ('rate_limit', 'restart_budget', 'restart_spacing', 'waiting'):
        parts.append('Cooldown expired; next check pending')
    if mode == 'rate_limit':
        parts.append('Waiting automatically; no restart needed')
    elif mode in ('restart_budget', 'restart_spacing'):
        parts.append('Runner is waiting and checking; progress saved')
    elif mode == 'restart_disabled':
        parts.append('Automatic restarts are set to 0')
    if detail and mode != 'rate_limit':
        parts.append(detail)
    verified, total = counts.get('Verified', 0), sum(counts.values())
    open_titles = sum(v for k, v in counts.items() if k not in DONE | {'Failed'})
    parts.append(f'{verified}/{total} titles verified; {open_titles} pending')
    return '[STATUS] ' + ' | '.join(parts)


class ApiError(Exception):
    def __init__(self, kind, status=0, retry_after=0):
        super().__init__(f"{kind} (HTTP {status})")
        self.kind, self.status, self.retry_after = kind, status, retry_after


class AccountChanged(Exception):
    pass


class AlreadyRunning(RuntimeError):
    pass


def xbox_process_ids():
    """Local process probe only: no Xbox requests and no account data."""
    import ctypes
    from ctypes import wintypes
    session = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise OSError('Could not determine the Windows session')
    result = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq XboxPcApp.exe', '/FO', 'CSV', '/NH'],
                            capture_output=True, text=True, check=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return tuple(sorted(int(row[1]) for row in csv.reader(result.stdout.splitlines())
                        if len(row) >= 4 and row[0].lower() == 'xboxpcapp.exe'
                        and row[1].isdigit() and row[3].isdigit() and int(row[3]) == session.value))


def bind_to_terminal(parent_pid):
    """Keep an OS handle to the launching shell; its exit ends this worker."""
    if not parent_pid:
        return
    if os.name != "nt":
        raise RuntimeError("Terminal binding is only supported on Windows.")
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE only
    if not handle:
        raise RuntimeError("The launching PowerShell window is no longer available.")

    def watch():
        result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        kernel.CloseHandle(handle)
        if result == 0:
            # Interrupt even an HTTP call or long rate-limit sleep. SQLite commits
            # remain durable; the next run verifies any InFlight job before writing.
            os._exit(130)

    threading.Thread(target=watch, daemon=True, name="terminal-lifetime").start()


def classify(status, body="", retry_after=0):
    text = str(body).lower()
    if status == 429 or "toomanyrequests" in text or "status code 429" in text:
        return ApiError("rate_limit", status, retry_after)
    if status in (401, 403) or any(s in text for s in
            ("unauthorized", "forbidden", "no xuid", "status code 401", "status code 403")):
        return ApiError("authentication", status, retry_after)
    if "notmodified" in text or status == 304:
        return ApiError("verify_required", status)
    if any(s in text for s in ("timed out", "timeout", "canceled", "connection", "socket")):
        return ApiError("connection", status)
    if "not found" in text or "no games" in text or status == 404:
        return ApiError("no_data", status)
    if status >= 500 or status == 408:
        return ApiError("server", status, retry_after)
    return ApiError("rejected", status)


def retry_seconds(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class Api:
    def __init__(self, base, timeout=150):
        self.base, self.timeout = base.rstrip("/"), timeout
        # Local requests must not go through a configured external proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data,
                                        headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ApiError("invalid_response")
            return result
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status, delay = exc.code, retry_seconds(exc.headers.get("Retry-After"))
            details = {}
            try:
                details = json.loads(raw)
                status = int(details.get("upstreamStatus") or status)
                delay = max(delay, retry_seconds(details.get("retryAfterSeconds")))
            except (ValueError, TypeError, AttributeError):
                details = {}
            error = classify(status, raw, delay)
            code = details.get('xboxErrorCode') if isinstance(details, dict) else None
            source = details.get('xboxErrorSource') if isinstance(details, dict) else None
            if type(code) is int:
                label = source if isinstance(source, str) and source.isascii() and source.isalnum() and len(source) <= 24 else 'Code'
                error.args = (f'{error} [Xbox {label} {code}]',)
            raise error from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            raise ApiError("connection") from None
        except (ValueError, UnicodeError):
            raise ApiError("invalid_response") from None

    def account(self):
        data = self.request("/api/profile/me", timeout=20)
        lower = {k.lower(): v for k, v in data.items()}
        people = lower.get("people") or lower.get("profileusers") or []
        if not people or not isinstance(people[0], dict):
            raise ApiError("authentication")
        user = {k.lower(): v for k, v in people[0].items()}
        xuid = str(user.get("xuid") or user.get("id") or "")
        if not xuid.isdigit():
            raise ApiError("authentication")
        return xuid

    def achievements(self, title):
        return self.request("/api/achievements/" + title)

    def unlock(self, title):
        return self.request("/api/achievements/unlockall", {"TitleId": title})


def inspect_achievements(data):
    items = data.get("achievements")
    if not isinstance(items, list) or not items:
        raise ApiError("no_data")
    if not all(isinstance(a, dict) and a.get("id") is not None for a in items):
        raise ApiError("invalid_response")
    if all("timeUnlocked" in a and "progressState" not in a for a in items):
        return "Unsupported", len(items), "Xbox360"
    if any(a.get("progressState") not in ("Achieved", "NotStarted", "InProgress") for a in items):
        raise ApiError("invalid_response")
    paging = data.get("pagingInfo") or {}
    if paging.get("continuationToken") or (len(items) >= 1000 and not data.get("allPagesFetched")):
        raise ApiError("incomplete_page")
    if all(a["progressState"] == "Achieved" for a in items):
        return "Verified", len(items), "All returned achievements are Achieved"
    return "Pending", len(items), "Achievements remain locked"


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
                attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', updated REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                time REAL NOT NULL, title TEXT, status TEXT NOT NULL, reason TEXT NOT NULL);
        """)

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def bind(self, xuid):
        previous = self.meta("xuid")
        if previous and previous != xuid:
            raise AccountChanged("Account changed. Use a separate --state file for each account.")
        self.meta("xuid", xuid)

    def seed(self, titles, retry_unresolved=False):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO jobs(id,name) VALUES (?,?)", titles)
            if retry_unresolved:
                self.db.execute("UPDATE jobs SET status='Pending',attempts=0,due=0 WHERE status='NeedsAttention'")

    def event(self, title, status, reason):
        with self.db:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (time.time(), title, status, reason))
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {title or '-'} {status}: {reason}", flush=True)

    def save(self, title, status, reason="", total=0, due=0, attempts=None):
        with self.db:
            self.db.execute("""UPDATE jobs SET status=?,reason=?,total=?,due=?,updated=?,
                               attempts=COALESCE(?,attempts) WHERE id=?""",
                            (status, reason, total, due, time.time(), attempts, title))
        self.event(title, status, reason)

    def next(self):
        return self.db.execute("""SELECT * FROM jobs WHERE status NOT IN ('Verified','Unsupported','NeedsAttention','Failed')
                                  ORDER BY due, rowid LIMIT 1""").fetchone()

    def counts(self):
        return dict(self.db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))

    def export(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(("TitleId", "GameTitle", "Status", "Attempts", "TotalAchievements", "Message"))
            writer.writerows(self.db.execute("SELECT id,name,status,attempts,total,reason FROM jobs ORDER BY rowid"))
        os.replace(temp, path)


class Runner:
    def __init__(self, api, state, args, sleep=time.sleep, now=time.time, restart=None, app_probe=None, app_restart=None):
        self.api, self.state, self.args = api, state, args
        self.sleep, self.now, self.restart = sleep, now, restart
        self.raw_sleep, self.app_probe = sleep, app_probe
        self.app_restart = app_restart or restart
        self.app_identity = None
        self.last_app_check = float('-inf')
        self.app_recovery_pending = False
        self.runtime_status = None
        self.last_status_emit = float('-inf')
        self.sleep = self.monitored_sleep

    def status(self, mode, detail='', until=0):
        status = {'mode': mode, 'detail': detail, 'until': until}
        changed = status != self.runtime_status
        self.runtime_status = status
        self.emit_status(force=changed)

    def emit_status(self, force=False):
        if self.runtime_status is None or (not force and self.now() - self.last_status_emit < 30):
            return
        self.last_status_emit = self.now()
        snapshot = {**self.runtime_status, 'updated': self.now()}
        cooldown = float(self.state.meta('cooldown_until') or 0)
        if cooldown > self.now() and snapshot['mode'] not in ('completed', 'paused', 'stopped'):
            snapshot.update(mode='rate_limit', until=cooldown)
        self.state.meta('runtime_status', json.dumps(snapshot, ensure_ascii=False))
        print(format_runtime_status(snapshot, self.state.counts(), self.now(), cooldown), flush=True)

    def try_restart(self, reason, restart):
        history = json.loads(self.state.meta('restarts') or '[]')
        history = [stamp for stamp in history if self.now() - stamp < 3600]
        if not restart:
            return False
        if self.args.max_restarts == 0:
            self.status('restart_disabled')
            return False
        budget_until = sorted(history)[-self.args.max_restarts] + 3600 if len(history) >= self.args.max_restarts else 0
        spacing_until = max(history) + 300 if history else 0
        if budget_until > self.now() or spacing_until > self.now():
            self.status('restart_budget' if budget_until > self.now() else 'restart_spacing',
                        f'{len(history)}/{self.args.max_restarts} restarts in the last hour',
                        max(budget_until, spacing_until))
            return False
        history.append(self.now())
        self.state.meta('restarts', json.dumps(history))
        self.state.event(None, 'Restart', reason)
        self.status('recovery', reason)
        try:
            restart()
        except (OSError, subprocess.SubprocessError) as error:
            self.state.event(None, 'RestartFailed', type(error).__name__ + ': Rechecking session')
            self.status('session', 'Recovery failed; an automatic retry will follow within the restart budget')
            return False
        self.app_identity = None  # Do not interpret our own restart as a new crash.
        self.last_app_check = self.now()
        self.status('session', 'Apps started; checking sign-in automatically')
        return True

    def check_app(self):
        if not self.app_probe or self.now() - self.last_app_check < 15:
            return
        self.last_app_check = self.now()
        try:
            identity = self.app_probe()
        except (OSError, subprocess.SubprocessError) as error:
            self.state.event(None, 'AppCheckFailed', type(error).__name__ + ': Process status unknown')
            return  # An uncertain probe must never trigger a restart.
        missing = not identity
        changed = bool(identity) and self.app_identity is not None and identity != self.app_identity
        if (missing or changed) and not self.app_recovery_pending:
            self.state.event(None, 'AppMissing' if missing else 'AppChanged',
                             'Xbox app is not running' if missing else 'Xbox app restarted; refreshing XAU session')
            self.app_recovery_pending = True
        if identity:
            self.app_identity = identity
        # Restarts never shorten a persisted account rate-limit pause.
        if self.app_recovery_pending and float(self.state.meta('cooldown_until') or 0) <= self.now():
            if self.try_restart('Open Xbox app and refresh XAU session', self.app_restart):
                self.app_recovery_pending = False

    def monitored_sleep(self, seconds):
        deadline = self.now() + seconds
        while self.now() < deadline:
            self.check_app()
            self.emit_status()
            remaining = deadline - self.now()
            if remaining > 0:
                self.raw_sleep(min(15, remaining))
        self.check_app()
        self.emit_status()

    def healthy(self, probe=False):
        self.check_app()
        if self.app_recovery_pending:
            raise ApiError('app_unavailable')
        account = self.api.account()
        excluded = getattr(self.args, 'source_account_sha256', None)
        if excluded and hashlib.sha256(account.encode()).hexdigest() == excluded:
            raise AccountChanged('Still signed in to the source account. Select the new account in the Xbox app and restart XAU.')
        self.state.bind(account)
        if probe:
            title = self.state.meta("probe_title") or self.args.probe_title
            inspect_achievements(self.api.achievements(title))

    def backoff(self, attempt, error):
        return max(error.retry_after, min(1800, self.args.retry_delay * 2 ** min(attempt - 1, 8))) + random.uniform(0, 3)

    def wait_health(self, probe=False):
        started, failures = self.now(), 0
        while True:
            cooldown = float(self.state.meta("cooldown_until") or 0) - self.now()
            if cooldown > 0:
                self.status('rate_limit', until=self.now() + cooldown)
                if self.args.health_wait and self.now() + cooldown - started >= self.args.health_wait:
                    raise RuntimeError("Server cooldown exceeds --health-wait; start again later.")
                self.sleep(cooldown)
            try:
                self.healthy(probe)
                if self.runtime_status and self.runtime_status['mode'] != 'processing':
                    self.status('processing', 'Session ready; resuming processing')
                return
            except ApiError as error:
                failures += 1
                if self.args.health_wait and self.now() - started >= self.args.health_wait:
                    raise RuntimeError("XAU remains unreachable or signed out. Progress saved; start again later.")
                self.state.event(None, "WaitingForSession", str(error))
                if error.kind == 'rate_limit':
                    delay = max(self.args.health_poll, error.retry_after)
                    self.state.meta('cooldown_until', self.now() + delay)
                    self.status('rate_limit', until=self.now() + delay)
                elif not self.runtime_status or self.runtime_status['mode'] not in ('restart_budget', 'restart_spacing', 'restart_disabled'):
                    detail = ('Xbox sign-in/session is not ready; automatic recovery is active. '
                              'If Microsoft displays a sign-in window, sign in there.') if error.kind == 'authentication' else 'Xbox connection is not ready; checking recovery'
                    self.status('session', detail)
                # Rate limits must be waited out, not countered by restarting.
                if error.kind not in ('rate_limit', 'app_unavailable') and failures >= 3:
                    self.try_restart('Restart XAU process and local API server', self.restart)
                delay = max(self.args.health_poll, error.retry_after)
                if error.kind == "rate_limit":
                    self.state.meta("cooldown_until", self.now() + delay)
                # Never send a new request before Retry-After, even if the health deadline is nearer.
                if self.args.health_wait and self.now() + delay - started >= self.args.health_wait:
                    raise RuntimeError("Wait limit reached; queue preserved.")
                self.sleep(delay)

    def verify(self, title, unsupported_ids=()):
        data = self.api.achievements(title)
        result = inspect_achievements(data)
        self.healthy()  # Never attribute the response to a changed account.
        self.observe_achievements(title, data, result)
        if result[0] != "Unsupported":
            self.state.meta("probe_title", title)
        if result[0] == "Pending" and unsupported_ids:
            remaining = {str(a["id"]) for a in data["achievements"] if a["progressState"] != "Achieved"}
            if remaining and remaining.issubset(set(map(str, unsupported_ids))):
                return "Unsupported", result[1], "Remaining achievements rejected by Xbox Progress code 49 (not updateable by this route)"
        return result

    def observe_achievements(self, title, data, result):
        """Optional consumer hook, only after response and account verification."""

    def before_unlock(self, title):
        """Optional consumer hook before an update attempt."""

    def process(self, job):
        title, attempt = job["id"], job["attempts"] + 1
        self.wait_health()
        self.status('processing', f"{job['name']} (ID {title})")
        self.state.save(title, "InFlight", "Read before write", attempts=attempt)
        try:
            status, total, reason = self.verify(title)
            if status in DONE:
                self.state.save(title, status, reason, total)
                return
            # InFlight is durable before submitting. A crash/timeout always resumes with a GET.
            post_error = None
            unsupported_ids = []
            try:
                self.before_unlock(title)
                receipt = self.api.unlock(title)
                if isinstance(receipt, dict) and isinstance(receipt.get("unsupportedAchievementIds"), list):
                    unsupported_ids = receipt["unsupportedAchievementIds"]
            except ApiError as error:
                post_error = error
                if error.kind not in ("verify_required", "connection", "server"):
                    raise
            for _ in range(self.args.verify_polls):
                self.sleep(self.args.verify_delay)
                status, total, reason = self.verify(title, unsupported_ids)
                if status in DONE:
                    self.state.save(title, status, reason, total)
                    return
            raise post_error or ApiError("not_verified")
        except ApiError as error:
            # Distinguish title errors from a broken session before consuming its retry budget.
            if error.kind in ("connection", "authentication", "server", "no_data", "invalid_response", "app_unavailable"):
                try:
                    self.healthy(probe=True)
                except ApiError as health_error:
                    # Persist the current title, then pause the whole queue.
                    self.state.save(title, "Retry", str(health_error), attempts=job["attempts"])
                    if health_error.kind == "rate_limit":
                        delay = self.backoff(attempt, health_error)
                        self.state.meta('cooldown_until', self.now() + delay)
                        self.status('rate_limit', until=self.now() + delay)
                        self.sleep(delay)
                    self.wait_health(probe=True)
                    return
            if error.kind == "rate_limit":
                delay = self.backoff(attempt, error)
                self.state.save(title, "Retry", str(error), due=self.now() + delay, attempts=job["attempts"])
                # A rate limit applies to the account, not just to this title.
                self.state.meta("cooldown_until", self.now() + delay)
                self.status('rate_limit', until=self.now() + delay)
                self.sleep(delay)
                return
            if attempt >= self.args.attempts:
                self.state.save(title, "NeedsAttention", str(error))
            else:
                self.state.save(title, "Retry", str(error), due=self.now() + self.backoff(attempt, error))

    def run(self):
        processed = 0
        while job := self.state.next():
            if self.args.max_titles and processed >= self.args.max_titles:
                break
            cooldown = float(self.state.meta("cooldown_until") or 0)
            wait = max(job["due"], cooldown) - self.now()
            if wait > 0:
                self.state.event(None, "Cooldown", f"{wait:.0f} seconds")
                self.status('rate_limit' if cooldown >= job['due'] else 'waiting', until=self.now() + wait)
                self.sleep(wait)
            self.process(job)
            self.state.export(self.args.report)
            processed += 1
            self.sleep(self.args.delay)
        return self.state.counts()


def load_titles(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        rows = csv.DictReader(source)
        if "ID" not in (rows.fieldnames or []):
            raise ValueError("CSV requires an ID column.")
        titles = {}
        for line, row in enumerate(rows, 2):
            title = (row.get("ID") or "").strip()
            if not title.isascii() or not title.isdigit() or not 0 < int(title) <= 4294967295:
                raise ValueError(f"Invalid title ID on line {line}")
            titles.setdefault(str(int(title)), row.get("Game Title") or "")
        return list(titles.items())


def load_verified_manifest(path):
    """Read an immutable selection of titles verified on the source account."""
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Invalid verified title list.')
    fingerprint = data.get('source_account_sha256', '')
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
        raise ValueError('Invalid account binding in title list.')
    rows = data.get('titles')
    if not isinstance(rows, list) or not rows:
        raise ValueError('The verified title list is empty.')
    titles = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('source_status') != 'Verified':
            raise ValueError('The separate list must contain only verified titles.')
        title = str(row.get('id', ''))
        if not title.isascii() or not title.isdigit() or not 0 < int(title) <= 4294967295:
            raise ValueError('Invalid title ID in the separate list.')
        title = str(int(title))
        if title in titles or not isinstance(row.get('name'), str):
            raise ValueError('Duplicate title ID or invalid title name.')
        titles[title] = row['name']
    return list(titles.items()), fingerprint


@contextlib.contextmanager
def endpoint_lock(base):
    # localhost and 127.0.0.1 denote the same server; use its port as lock identity.
    identity = str(urllib.parse.urlparse(base).port or 80)
    path = Path(tempfile.gettempdir()) / ("xau-runner-" + hashlib.sha256(identity.encode()).hexdigest()[:16] + ".lock")
    with path.open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AlreadyRunning("A runner is already using this XAU port.") from None
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def restart_xau(exe, port, restart_xbox=True):
    script = Path(__file__).with_name("Restart-Xau.ps1")
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
               "-ExePath", str(exe), "-Port", str(port), "-StartXboxApp", "-ShowWindow"]
    if restart_xbox:
        command.append('-RestartXboxApp')
    subprocess.run(command, check=True, timeout=90)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", type=Path, default=ROOT / "IDs_new.csv")
    p.add_argument("--verified-manifest", type=Path, help="Separate JSON selection of previously verified titles")
    p.add_argument("--state", type=Path, default=ROOT / "outputs/automation/state.sqlite")
    p.add_argument("--report", type=Path, default=ROOT / "outputs/automation/results.csv")
    p.add_argument("--base", default="http://localhost:1337")
    p.add_argument("--exe", type=Path, default=ROOT / "automation/bin/XAU.exe")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--status", action="store_true", help="Show saved progress without starting work")
    p.add_argument("--parent-pid", type=int, default=0, help="Stop when the launching PowerShell process exits")
    p.add_argument("--auto-restart", action="store_true")
    p.add_argument("--retry-unresolved", action="store_true")
    p.add_argument("--max-titles", type=int, default=0, help="Maximum number of queue steps (0 = unlimited)")
    p.add_argument("--attempts", type=int, default=4)
    p.add_argument("--delay", type=float, default=3)
    p.add_argument("--retry-delay", type=float, default=30)
    p.add_argument("--verify-delay", type=float, default=10)
    p.add_argument("--verify-polls", type=int, default=3)
    p.add_argument("--health-poll", type=float, default=30)
    p.add_argument("--health-wait", type=float, default=1800, help="Session wait limit in seconds; 0 waits indefinitely")
    p.add_argument("--max-restarts", type=int, default=3)
    p.add_argument("--probe-title", default="978050046", help="Known readable achievement title for outage checks")
    args = p.parse_args(argv)
    if not args.probe_title.isascii() or not args.probe_title.isdigit() or not 0 < int(args.probe_title) <= 4294967295:
        p.error("--probe-title must be a valid title ID.")
    url = urllib.parse.urlparse(args.base)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
        p.error("--base must be a local HTTP address.")
    for name in ("attempts", "verify_polls", "health_poll", "retry_delay"):
        if getattr(args, name) <= 0:
            p.error(f"--{name.replace('_', '-')} must be positive.")
    for name in ("delay", "verify_delay", "max_titles", "max_restarts", "health_wait"):
        if getattr(args, name) < 0:
            p.error(f"--{name.replace('_', '-')} must not be negative.")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.status:
        show_status(args.state)
        return 0
    if args.verified_manifest:
        titles, args.source_account_sha256 = load_verified_manifest(args.verified_manifest)
    else:
        titles = load_titles(args.csv)
    print(f"{len(titles)} unique titles; database: {args.state}")
    if not args.execute:
        print("Preview: no API calls, no unlocks, no restarts.")
        if args.state.exists():
            with contextlib.closing(sqlite3.connect(args.state.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                print(dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")))
        return 0
    bind_to_terminal(args.parent_pid)
    if args.auto_restart and not args.exe.is_file():
        raise ValueError("Patched XAU.exe is missing. Run Build-Xau.ps1 first or specify --exe.")
    with endpoint_lock(args.base):
        state = State(args.state)
        runner = None
        try:
            state.seed(titles, args.retry_unresolved)
            api = Api(args.base)
            restart = (lambda: restart_xau(args.exe.resolve(), urllib.parse.urlparse(args.base).port or 80)) if args.auto_restart else None
            app_probe = xbox_process_ids if args.auto_restart and os.name == 'nt' else None
            app_restart = (lambda: restart_xau(args.exe.resolve(), urllib.parse.urlparse(args.base).port or 80, restart_xbox=False)) if app_probe else None
            runner = Runner(api, state, args, restart=restart, app_probe=app_probe, app_restart=app_restart)
            if app_probe:
                state.event(None, 'AppMonitorStarted', 'Local Xbox process check active every 15 seconds between requests and during cooldowns')
            state.event(None, "RunStarted", "Resuming queue from saved progress")
            runner.status('session', 'Checking XAU and Xbox sign-in')
            counts = runner.run()
            print(json.dumps(counts, ensure_ascii=False))
            unresolved = sum(value for key, value in counts.items() if key not in DONE)
            if state.next() is None:
                runner.status('completed', f'Queue processed; {unresolved} titles remain unverified')
                state.event(None, "RunCompleted", f"Queue processed; {unresolved} titles remain unverified. No crash occurred.")
            else:
                runner.status('paused', 'Step limit reached; use the same launcher to resume')
                state.event(None, "RunPaused", "Step limit reached; progress saved")
            return 0 if all(key in DONE for key in counts) else 2
        except Exception as error:
            if runner:
                runner.status('stopped', str(error))
            state.event(None, "RunStopped", type(error).__name__)
            raise
        except KeyboardInterrupt:
            if runner:
                runner.status('paused', 'Canceled by user; use the same launcher to resume')
            state.event(None, "RunPaused", "Canceled by user; Progress saved")
            raise
        finally:
            state.export(args.report)
            state.db.close()


def show_status(path):
    path = Path(path).resolve()
    if not path.exists():
        print("No saved progress yet.")
        return
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        counts = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        saved = db.execute("SELECT value FROM meta WHERE key='runtime_status'").fetchone()
        cooldown_row = db.execute("SELECT value FROM meta WHERE key='cooldown_until'").fetchone()
        if saved:
            snapshot = json.loads(saved[0])
            print(format_runtime_status(snapshot, counts, time.time(), float(cooldown_row[0]) if cooldown_row else 0))
            print(f"Status saved {max(0, int(time.time()-snapshot['updated']))} seconds ago; no live process check.")
        print("Progress: " + ", ".join(f"{key}: {value}" for key, value in counts.items()))
        cooldown = db.execute("SELECT value FROM meta WHERE key='cooldown_until'").fetchone()
        remaining = max(0, math.ceil(float(cooldown[0]) - time.time())) if cooldown else 0
        if remaining and (not saved or snapshot['mode'] not in ('completed', 'paused', 'stopped')):
            minutes, seconds = divmod(remaining, 60)
            print(f"Xbox rate limit: cooldown remaining {minutes} min {seconds:02d} sec")
            print("An active runner will retry automatically after the cooldown. No restart required.")
        row = db.execute("SELECT time,title,status,reason FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
        if row:
            print(f"Last activity {max(0, int(time.time()-row[0]))} seconds ago: {row[2]} - {row[3]}")
        print(f"Database: {path}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Stopped. The next start will resume from the database.")
        sys.exit(130)
    except AlreadyRunning:
        print("Automation is already running. A second runner will not be started.")
        show_status(parse_args().state)
        sys.exit(0)
    except (AccountChanged, RuntimeError, ValueError, sqlite3.Error, OSError, subprocess.SubprocessError) as error:
        print(f"STOP: {error}", file=sys.stderr)
        sys.exit(2)
