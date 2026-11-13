import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

import worker


class StudioWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'studio/state.sqlite'
        self.manifest = self.root / 'verified.json'
        self.manifest.write_text(json.dumps({'schema_version': 1, 'source_account_sha256': hashlib.sha256(b'old').hexdigest(), 'titles': [{'id': '7', 'name': 'Seven', 'source_status': 'Verified'}]}))
        self.calls = []
        self.unlocked = False
        self.unlocked_titles = set()
        self.achievement_ids = ['a']
        self.mode = 'normal'
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                test.calls.append(('GET', self.path, self.headers.get('X-Studio-Key')))
                if self.path == '/api/profile/me':
                    body = {'profileUsers': [{'id': '999' if test.mode == 'wrong_account' else '123'}]}
                else:
                    unlocked = test.unlocked or self.path.rsplit('/', 1)[-1] in test.unlocked_titles
                    body = {'achievements': [{'id': a, 'progressState': 'Achieved' if unlocked else 'NotStarted'} for a in test.achievement_ids]}
                if test.mode == 'rate_limit':
                    self.send_response(429)
                    self.send_header('Retry-After', '120')
                    (test.root / 'pause.flag').write_text('pause')
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                test.calls.append(('POST', self.path, self.headers.get('X-Studio-Key')))
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                test.unlocked_titles.add(body['TitleId'])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_worker(self, random_titles=False):
        args = worker.queue.parse_args(['--state', str(self.state), '--report', str(self.root / 'results.csv'), '--verified-manifest', str(self.manifest), '--base', f'http://127.0.0.1:{self.server.server_port}', '--verify-delay', '0', '--delay', '0', '--health-wait', '0'])
        with patch.object(worker, 'ROOT', self.root), patch.object(worker.queue, 'parse_args', return_value=args), patch.object(worker.queue, 'endpoint_lock', lambda _: contextlib.nullcontext()), patch.dict(os.environ, XAU_STUDIO_KEY='test-key', XAU_STUDIO_ACCOUNT='123', XAU_STUDIO_STOP=str(self.root / 'pause.flag'), XAU_STUDIO_RANDOM_TITLES='1' if random_titles else '0'):
            return worker.main()

    def read_meta(self, key):
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            return db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]

    def test_verified_write_read_and_resume(self):
        self.assertEqual(self.run_worker(), 0)
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'Verified')
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.calls), 1)
        self.assertTrue(all(key == 'test-key' for _, _, key in self.calls))
        self.assertEqual(json.loads(self.read_meta('studio_run_stats'))['newly_completed'], 1)
        before = len(self.calls)
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(json.loads(self.read_meta('studio_run_stats'))['newly_completed'], 0)

    def test_already_complete_title_is_not_newly_completed(self):
        self.unlocked = True
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(json.loads(self.read_meta('studio_run_stats'))['newly_completed'], 0)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.calls))

    def test_no_bundled_catalogue_processes_account_csv_import(self):
        self.manifest.unlink()
        self.state.parent.mkdir(parents=True)
        (self.state.parent / 'additional-titles.json').write_text(json.dumps([{'Id': '7', 'Name': 'User imported example'}]))
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.unlocked_titles, {'7'})
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'Verified')

    def test_empty_installation_makes_no_service_requests(self):
        self.manifest.unlink()
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.calls, [])

    def test_separate_process_restart_preserves_verified_and_skips_requests(self):
        self.assertEqual(self.run_worker(), 0)
        env = dict(os.environ, XAU_STUDIO_ACCOUNT='123', XAU_STUDIO_KEY='test-key',
                   XAU_STUDIO_STOP=str(self.root / 'pause.flag'))
        result = subprocess.run([sys.executable, str(Path(worker.__file__).resolve()),
            '--state', str(self.state), '--report', str(self.root / 'results.csv'),
            '--verified-manifest', str(self.manifest),
            '--base', f'http://127.0.0.1:{self.server.server_port}', '--delay', '0'],
            env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.calls), 1)
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'Verified')

    def test_wrong_account_never_writes(self):
        self.mode = 'wrong_account'
        with self.assertRaises(worker.queue.AccountChanged):
            self.run_worker()
        self.assertFalse(any(method == 'POST' for method, _, _ in self.calls))
        self.assertEqual(json.loads(self.read_meta('studio_run_stats'))['newly_completed'], 0)

    def test_pause_during_rate_limit_persists_cooldown(self):
        self.mode = 'rate_limit'
        self.assertEqual(self.run_worker(), 0)
        self.assertGreater(float(self.read_meta('cooldown_until')), worker.time.time() + 110)
        self.assertEqual(json.loads(self.read_meta('runtime_status'))['mode'], 'paused')
        self.assertEqual(len(self.calls), 1)

    def test_import_matching_account_and_preserve_excluded(self):
        previous = self.root / 'outputs/automation/verified-account/state.sqlite'
        db = worker.queue.State(previous)
        db.bind('123')
        db.seed([('7', 'Seven')], False)
        db.save('7', 'Failed', 'User excluded')
        db.db.close()
        self.assertEqual(self.run_worker(), 0)
        with contextlib.closing(sqlite3.connect(self.state)) as result:
            self.assertEqual(result.execute('SELECT status FROM jobs').fetchone()[0], 'Failed')
        self.assertEqual(self.calls, [])

    def test_other_account_progress_not_imported(self):
        previous = self.root / 'outputs/automation/verified-account/state.sqlite'
        db = worker.queue.State(previous)
        db.bind('456')
        db.seed([('7', 'Seven')], False)
        db.save('7', 'Failed', 'Other account')
        db.db.close()
        self.assertEqual(self.run_worker(), 0)
        self.assertTrue(any(method == 'POST' for method, _, _ in self.calls))

    def test_random_mode_finishes_all_achievements_before_next_game(self):
        manifest = json.loads(self.manifest.read_text())
        manifest['titles'].append({'id': '8', 'name': 'Eight', 'source_status': 'Verified'})
        self.manifest.write_text(json.dumps(manifest))
        self.achievement_ids = ['a', 'b', 'c']
        self.assertEqual(self.run_worker(random_titles=True), 0)
        reads = [path.rsplit('/', 1)[-1] for method, path, _ in self.calls if method == 'GET' and '/achievements/' in path]
        self.assertIn(reads, [['7', '7', '8', '8'], ['8', '8', '7', '7']])
        self.assertEqual(self.unlocked_titles, {'7', '8'})
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.calls), 2)
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='Verified' AND total=3").fetchone()[0], 2)
        self.assertEqual(json.loads(self.read_meta('studio_run_stats'))['newly_completed'], 2)


if __name__ == '__main__':
    unittest.main()
