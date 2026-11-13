"""Owned XAU Studio worker. Uses the existing verified queue without app automation."""
import os
from pathlib import Path
import sys
import time
import json
import sqlite3
import contextlib
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'automation'))
import xau_runner as queue
from run_stats import RunStats


class StudioState(queue.State):
    def __init__(self, path, random_titles=False):
        super().__init__(path)
        self.random_titles = random_titles

    def next(self):
        if not self.random_titles:
            return super().next()
        # Randomize only eligible titles. An interrupted title is verified first;
        # future retries never delay other ready work or bypass their due time.
        job = self.db.execute('''SELECT * FROM jobs
            WHERE status NOT IN ('Verified','Unsupported','NeedsAttention','Failed') AND due<=?
            ORDER BY CASE WHEN status='InFlight' THEN 0 ELSE 1 END, RANDOM() LIMIT 1''',
            (time.time(),)).fetchone()
        return job if job is not None else super().next()

    def repair_update_schema_failures(self):
        # The original Studio update payload omitted the required action field.
        # Only repair its recoverable 400 failures; never reopen user exclusions.
        if self.meta('studio_progress_update_schema') == '1':
            return 0
        with self.db:
            count = self.db.execute("""UPDATE jobs SET status='Pending',attempts=0,due=0,
                reason='Rescheduled: update request format corrected'
                WHERE status IN ('Retry','NeedsAttention') AND reason='rejected (HTTP 400)'""").rowcount
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('studio_progress_update_schema', '1'))
        if count:
            self.event(None, 'RequestFormatFixed', f'{count} titles blocked by HTTP 400 rescheduled')
        return count


class StudioRunner(queue.Runner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_stats = RunStats(self.state)

    def observe_achievements(self, title, data, result):
        self.run_stats.observe(title, data, result)

    def before_unlock(self, title):
        self.run_stats.before_unlock(title)


class KeyHandler(urllib.request.BaseHandler):
    def http_request(self, request):
        request.add_header('X-Studio-Key', os.environ['XAU_STUDIO_KEY'])
        return request


def main():
    args = queue.parse_args()
    stop = Path(os.environ['XAU_STUDIO_STOP'])

    def check_stop():
        if stop.exists():
            raise KeyboardInterrupt()

    def sleep(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            check_stop()
            time.sleep(min(.25, max(0, end - time.monotonic())))
        check_stop()

    class StudioApi(queue.Api):
        def request(self, *a, **kw):
            check_stop()
            return super().request(*a, **kw)

    queue.bind_to_terminal(args.parent_pid)
    # Also honor the old launcher's lock: two apps must never process the same account concurrently.
    with queue.endpoint_lock('http://localhost:1337'), queue.endpoint_lock(args.base):
        account = os.environ['XAU_STUDIO_ACCOUNT']
        if not args.state.exists():
            for previous in (ROOT / 'outputs/automation/verified-account/state.sqlite', ROOT / 'outputs/automation/state.sqlite'):
                if not previous.exists():
                    continue
                with contextlib.closing(sqlite3.connect(previous.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                    identity = source.execute("SELECT value FROM meta WHERE key='xuid'").fetchone()
                    if identity and identity[0] == account:
                        args.state.parent.mkdir(parents=True, exist_ok=True)
                        with contextlib.closing(sqlite3.connect(args.state)) as target:
                            source.backup(target)
                        print('[STUDIO] Previous account progress imported safely.', flush=True)
                        break
        random_titles = os.environ.get('XAU_STUDIO_RANDOM_TITLES') == '1'
        state = StudioState(args.state, random_titles=random_titles)
        runner = None
        try:
            titles = []
            if args.verified_manifest and args.verified_manifest.exists():
                titles, _ = queue.load_verified_manifest(args.verified_manifest)
            extra = args.state.parent / 'additional-titles.json'
            if extra.exists():
                for row in json.loads(extra.read_text(encoding='utf-8-sig')):
                    title = row['Id']
                    if not isinstance(title, str) or not title.isascii() or not title.isdigit() or not 0 < int(title) <= 4294967295:
                        raise ValueError('Invalid additional title ID')
                    titles.append((str(int(title)), row['Name']))
            state.bind(account)
            state.seed(titles, args.retry_unresolved)
            state.repair_update_schema_failures()
            if not state.counts():
                state.event(None, 'EmptyQueue', 'No title list is bundled. Sign in and import your own CSV in Settings before starting.')
            api = StudioApi(args.base)
            api.opener.add_handler(KeyHandler())
            runner = StudioRunner(api, state, args, sleep=sleep)
            runner.status('session', 'Checking direct Microsoft/Xbox session')
            state.event(None, 'RunStarted', 'XAU Studio; Xbox app not required')
            state.event(None, 'TitleOrder', 'Random title order; all achievements per game' if random_titles else 'List order; all achievements per game')
            counts = runner.run()
            runner.status('completed' if state.next() is None else 'paused', 'Progress saved')
            state.event(None, 'RunCompleted' if state.next() is None else 'RunPaused', str(counts))
            return 0
        except KeyboardInterrupt:
            if runner:
                runner.status('paused', 'Paused by user; safe to resume')
            state.event(None, 'RunPaused', 'Canceled by user')
            return 0
        except Exception as error:
            if runner:
                runner.status('stopped', type(error).__name__)
            state.event(None, 'RunStopped', type(error).__name__)
            raise
        finally:
            state.export(args.report)
            state.db.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except queue.AlreadyRunning:
        print('[STUDIO] A previous XAU run is still active. Close its terminal, then start here.', flush=True)
        sys.exit(3)
    except Exception as error:
        print('[STUDIO] Run stopped: ' + type(error).__name__, flush=True)
        sys.exit(1)
