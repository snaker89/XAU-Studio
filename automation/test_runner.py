import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import xau_runner as x


class ApiDiagnosticsTests(unittest.TestCase):
    def test_service_error_code_is_preserved_without_raw_response(self):
        api = x.Api('http://127.0.0.1:1337')
        body = json.dumps({'upstreamStatus': 400, 'xboxErrorCode': 6, 'xboxErrorSource': 'Progress', 'error': 'sensitive response not for logs'}).encode()
        api.opener = Mock()
        api.opener.open.side_effect = x.urllib.error.HTTPError(api.base, 400, 'Bad Request', {}, io.BytesIO(body))
        with self.assertRaises(x.ApiError) as caught:
            api.unlock('123')
        self.assertEqual(str(caught.exception), 'rejected (HTTP 400) [Xbox Progress 6]')
        self.assertNotIn('sensitive', str(caught.exception))


def achievements(achieved=False):
    return {"achievements": [{"id": "1", "progressState": "Achieved" if achieved else "NotStarted"}]}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.state = x.State(self.path / "state.sqlite")
        self.addCleanup(self.state.db.close)
        self.state.seed([("123", "Example"), ("456", "Other")])
        self.api = Mock()
        self.api.account.return_value = "1111"
        self.args = x.parse_args(["--state", str(self.path / "state.sqlite"), "--report", str(self.path / "report.csv")])
        self.args.delay = 0
        self.t = 10000
        self.restart = Mock()
        self.runner = x.Runner(self.api, self.state, self.args, sleep=self.sleep, now=lambda: self.t, restart=self.restart)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def sleep(self, seconds):
        self.t += seconds

    def row(self):
        return self.state.db.execute("SELECT * FROM jobs WHERE id='123'").fetchone()

    def test_already_achieved_does_not_post(self):
        self.api.achievements.return_value = achievements(True)
        self.runner.process(self.row())
        self.api.unlock.assert_not_called()
        self.assertEqual(self.row()["status"], "Verified")

    def test_success_must_be_verified(self):
        self.api.achievements.return_value = achievements(False)
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Retry")

    def test_code49_only_unsupported_after_reading_remaining_ids(self):
        self.api.achievements.return_value = achievements(False)
        self.api.unlock.return_value = {"unsupportedAchievementIds": ["1"]}
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Unsupported")
        self.assertIn("code 49", self.row()["reason"])
        self.assertEqual(self.api.achievements.call_count, 2)

    def test_code49_does_not_hide_other_locked_achievements(self):
        self.api.achievements.return_value = {"achievements": [
            {"id": "1", "progressState": "NotStarted"},
            {"id": "2", "progressState": "NotStarted"}]}
        self.api.unlock.return_value = {"unsupportedAchievementIds": ["1"]}
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Retry")

    def test_verified_wins_if_unsupported_achievement_has_since_unlocked(self):
        self.api.achievements.side_effect = [achievements(False), achievements(True)]
        self.api.unlock.return_value = {"unsupportedAchievementIds": ["1"]}
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Verified")

    def test_eventual_consistency(self):
        self.api.achievements.side_effect = [achievements(), achievements(), achievements(True)]
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Verified")
        self.api.unlock.assert_called_once()

    def test_ambiguous_timeout_is_verified_before_retry(self):
        self.api.unlock.side_effect = x.ApiError("connection")
        self.api.achievements.side_effect = [achievements(), achievements(True)]
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Verified")
        self.api.unlock.assert_called_once()

    def test_not_modified_is_not_blind_success(self):
        self.api.unlock.side_effect = x.classify(500, "NotModified")
        self.api.achievements.return_value = achievements()
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Retry")

    def test_rate_limit_global_cooldown_persists(self):
        self.api.achievements.side_effect = x.ApiError("rate_limit", 429, 123)
        self.runner.process(self.row())
        self.assertGreaterEqual(self.t, 10123)
        self.assertGreaterEqual(float(self.state.meta("cooldown_until")), 10123)
        self.assertEqual(self.row()["attempts"], 0)
        self.restart.assert_not_called()

    def test_health_rate_limit_never_restarts(self):
        self.api.account.side_effect = [x.ApiError("rate_limit", 429, 90)] * 4 + ["1111"]
        self.runner.wait_health()
        self.assertEqual(self.t, 10360)
        self.restart.assert_not_called()

    def monitored_runner(self, probe):
        return x.Runner(self.api, self.state, self.args, sleep=self.sleep, now=lambda: self.t,
                        restart=self.restart, app_probe=probe, app_restart=self.restart)

    def test_missing_xbox_is_recovered_before_account_requests(self):
        runner = self.monitored_runner(Mock(return_value=()))
        runner.healthy()
        self.restart.assert_called_once()
        self.assertFalse(runner.app_recovery_pending)
        self.api.account.assert_called_once()
        self.api.unlock.assert_not_called()

    def test_pid_change_during_sleep_recovers_without_xbox_api_calls(self):
        probe = Mock(side_effect=[(10,), (20,), (20,), (20,)])
        runner = self.monitored_runner(probe)
        runner.sleep(45)
        self.restart.assert_called_once()
        self.api.account.assert_not_called()
        self.api.unlock.assert_not_called()
        self.assertEqual(self.state.counts(), {'Pending': 2})

    def test_process_monitor_preserves_cooldown_and_waits_to_recover(self):
        runner = self.monitored_runner(Mock(return_value=()))
        self.state.meta('cooldown_until', self.t + 120)
        runner.sleep(30)
        self.restart.assert_not_called()
        self.api.account.assert_not_called()
        self.assertEqual(float(self.state.meta('cooldown_until')), 10120)
        runner.sleep(90)
        self.restart.assert_called_once()
        self.assertEqual(self.t, 10120)
        self.api.account.assert_not_called()

    def test_process_monitor_obeys_shared_persisted_budget(self):
        runner = self.monitored_runner(Mock(return_value=()))
        self.state.meta('restarts', json.dumps([9990, 9980, 9970]))
        with self.assertRaises(x.ApiError) as caught:
            runner.healthy()
        self.assertEqual(caught.exception.kind, 'app_unavailable')
        self.restart.assert_not_called()
        self.api.account.assert_not_called()
        self.assertEqual(self.state.counts(), {'Pending': 2})

    def test_process_probe_failure_does_not_trigger_restart(self):
        runner = self.monitored_runner(Mock(side_effect=x.subprocess.TimeoutExpired('tasklist', 10)))
        runner.healthy()
        self.restart.assert_not_called()
        self.api.account.assert_called_once()
        self.assertEqual(self.state.db.execute("select count(*) from events where status='AppCheckFailed'").fetchone()[0], 1)

    def test_failed_process_recovery_keeps_queue_paused_and_rate_limited(self):
        runner = self.monitored_runner(Mock(return_value=()))
        self.restart.side_effect = OSError('cannot start')
        runner.sleep(60)
        self.restart.assert_called_once()
        self.assertTrue(runner.app_recovery_pending)
        self.api.account.assert_not_called()
        self.assertEqual(self.state.counts(), {'Pending': 2})

    def test_unlimited_session_wait_resumes_after_long_outage(self):
        self.args.health_wait = 0
        self.api.account.side_effect = [x.ApiError("rate_limit", 429, 3600), "1111"]
        self.runner.wait_health()
        self.assertEqual(self.t, 13600)
        self.restart.assert_not_called()
        self.assertEqual(x.parse_args(['--health-wait', '0']).health_wait, 0)

    def test_session_outage_does_not_mark_following_titles_failed(self):
        self.args.health_wait = 130
        self.api.account.side_effect = x.ApiError("connection")
        with self.assertRaises(RuntimeError):
            self.runner.run()
        self.assertEqual(self.state.counts(), {"Pending": 2})
        self.restart.assert_called_once()

    def test_restart_budget_persists(self):
        self.state.meta("restarts", json.dumps([9990, 9980, 9970]))
        self.args.health_wait = 130
        self.api.account.side_effect = x.ApiError("connection")
        with self.assertRaises(RuntimeError):
            self.runner.wait_health()
        self.restart.assert_not_called()

    def test_failed_restart_waits_and_recovers_without_losing_jobs(self):
        self.api.account.side_effect = [x.ApiError("connection")] * 3 + ["1111"]
        self.restart.side_effect = x.subprocess.TimeoutExpired("recovery", 60)
        self.runner.wait_health()
        self.restart.assert_called_once()
        self.assertEqual(self.state.counts(), {"Pending": 2})
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM events WHERE status='RestartFailed'").fetchone()[0], 1)

    def test_failed_restarts_still_obey_budget(self):
        self.args.health_wait = 0
        self.api.account.side_effect = [x.ApiError("connection")] * 130 + ["1111"]
        self.restart.side_effect = OSError("cannot start")
        self.runner.wait_health()
        self.assertLessEqual(self.restart.call_count, 6)
        self.assertEqual(self.state.counts(), {"Pending": 2})

    def test_unresolved_queue_has_explicit_completion_event(self):
        csv = self.path / "titles.csv"
        csv.write_text("ID,Game Title\n123,Example\n456,Other\n", encoding="utf-8")
        with self.state.db:
            self.state.db.execute("UPDATE jobs SET status='NeedsAttention',reason='no_data'")
        with patch.object(x, "Api") as api:
            code = x.main(["--execute", "--csv", str(csv), "--state", str(self.path / "state.sqlite"),
                           "--report", str(self.path / "report.csv"), "--base", "http://localhost:31480"])
        self.assertEqual(code, 2)
        api.return_value.account.assert_not_called()
        event = self.state.db.execute("SELECT status,reason FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(event["status"], "RunCompleted")
        self.assertIn("2 Titel", event["reason"])

    def test_session_outage_during_title_keeps_attempt(self):
        self.api.account.side_effect = ["1111", x.ApiError("authentication"), "1111"]
        self.api.achievements.side_effect = [x.ApiError("connection"), achievements()]
        self.runner.process(self.row())
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(self.row()["status"], "Retry")

    def test_wrong_account_stops_before_post(self):
        self.state.bind("2222")
        with self.assertRaises(x.AccountChanged):
            self.runner.process(self.row())
        self.api.unlock.assert_not_called()

    def test_verified_selection_rejects_source_account_before_binding_or_post(self):
        self.args.source_account_sha256 = x.hashlib.sha256(b'1111').hexdigest()
        with self.assertRaises(x.AccountChanged):
            self.runner.process(self.row())
        self.assertIsNone(self.state.meta('xuid'))
        self.assertEqual(self.row()['status'], 'Pending')
        self.api.unlock.assert_not_called()

    def test_verified_manifest_rejects_unverified_and_duplicate_titles(self):
        manifest = self.path / 'verified.json'
        data = {'schema_version': 1, 'source_account_sha256': x.hashlib.sha256(b'2222').hexdigest(),
                'titles': [{'id': '123', 'name': 'Example', 'source_status': 'Verified'}]}
        manifest.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(x.load_verified_manifest(manifest)[0], [('123', 'Example')])
        data['titles'][0]['source_status'] = 'Unsupported'
        manifest.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            x.load_verified_manifest(manifest)
        data['titles'][0]['source_status'] = 'Verified'
        data['titles'] *= 2
        manifest.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            x.load_verified_manifest(manifest)

    def test_new_account_selection_uses_new_state_and_own_report(self):
        manifest = self.path / 'verified.json'
        manifest.write_text(json.dumps({'schema_version': 1,
            'source_account_sha256': x.hashlib.sha256(b'2222').hexdigest(),
            'titles': [{'id': '789', 'name': 'Selected', 'source_status': 'Verified'}]}), encoding='utf-8')
        destination = self.path / 'new-account/state.sqlite'
        report = self.path / 'new-account/results.csv'
        api = Mock()
        api.account.return_value = '3333'
        api.achievements.side_effect = [achievements(False), achievements(True)]
        with patch.object(x, 'Api', return_value=api), patch.object(x.time, 'sleep'):
            code = x.main(['--execute', '--verified-manifest', str(manifest), '--state', str(destination),
                           '--report', str(report), '--base', 'http://localhost:31481', '--verify-delay', '0', '--delay', '0'])
        self.assertEqual(code, 0)
        api.unlock.assert_called_once_with('789')
        with contextlib.closing(x.sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute('select id,status from jobs').fetchall(), [('789', 'Verified')])
            self.assertEqual(db.execute("select value from meta where key='xuid'").fetchone()[0], '3333')
        self.assertEqual(self.state.counts(), {'Pending': 2})
        self.assertIsNone(self.state.meta('xuid'))
        self.assertIn('Selected', report.read_text(encoding='utf-8-sig'))

    def test_account_switch_during_read_stops_before_post(self):
        self.api.account.side_effect = ["1111", "2222"]
        self.api.achievements.return_value = achievements()
        with self.assertRaises(x.AccountChanged):
            self.runner.process(self.row())
        self.api.unlock.assert_not_called()

    def test_crash_resume_verifies_inflight(self):
        self.state.save("123", "InFlight", attempts=1)
        self.state.db.close()
        self.state = x.State(self.path / "state.sqlite")
        self.addCleanup(self.state.db.close)
        self.runner.state = self.state
        self.api.achievements.return_value = achievements(True)
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Verified")
        self.api.unlock.assert_not_called()

    def test_retry_budget_then_explicit_requeue(self):
        def read(title):
            if title == "123":
                raise x.ApiError("no_data")
            return achievements()
        self.api.achievements.side_effect = read
        for _ in range(self.args.attempts):
            self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "NeedsAttention")
        self.state.seed([], retry_unresolved=True)
        self.assertEqual(self.row()["status"], "Pending")
        self.assertEqual(self.row()["attempts"], 0)

    def test_user_failed_titles_are_never_automatically_requeued(self):
        self.state.save("123", "Failed", "Vom Benutzer dauerhaft als fehlerhaft markiert")
        self.state.save("456", "NeedsAttention", "no_data")
        self.state.seed([("123", "Example"), ("789", "New title")], retry_unresolved=True)
        self.assertEqual(self.row()["status"], "Failed")
        pending = []
        while job := self.state.next():
            pending.append(job["id"])
            self.state.save(job["id"], "Verified")
        self.assertEqual(set(pending), {"456", "789"})
        self.assertEqual(self.row()["reason"], "Vom Benutzer dauerhaft als fehlerhaft markiert")

    def test_achievement_service_outage_pauses_even_if_profile_works(self):
        self.args.health_wait = 130
        self.api.achievements.side_effect = x.ApiError("server", 503)
        with self.assertRaises(RuntimeError):
            self.runner.run()
        self.assertEqual(self.row()["status"], "Retry")
        self.assertEqual(self.row()["attempts"], 0)
        other = self.state.db.execute("SELECT status FROM jobs WHERE id='456'").fetchone()[0]
        self.assertEqual(other, "Pending")

    def test_last_readable_title_becomes_outage_probe(self):
        self.api.achievements.return_value = achievements(True)
        self.runner.process(self.row())
        self.assertEqual(self.state.meta("probe_title"), "123")

    def test_360_never_posts(self):
        self.api.achievements.return_value = {"achievements": [{"id": 1, "timeUnlocked": ""}]}
        self.runner.process(self.row())
        self.assertEqual(self.row()["status"], "Unsupported")
        self.api.unlock.assert_not_called()

    def test_empty_and_incomplete_lists_not_verified(self):
        for data in ({"achievements": []}, {**achievements(True), "pagingInfo": {"continuationToken": "next"}},
                     {"achievements": achievements(True)["achievements"] * 1000}):
            with self.assertRaises(x.ApiError):
                x.inspect_achievements(data)

    def test_csv_deduplicates_and_rejects_invalid(self):
        path = self.path / "input.csv"
        path.write_text('ID,Game Title\n123,One\n123,Again\n', encoding="utf-8")
        self.assertEqual(x.load_titles(path), [("123", "One")])
        path.write_text('ID,Game Title\nbad,One\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            x.load_titles(path)

    def test_export_and_resume_do_not_skip_errors(self):
        self.state.save("123", "Retry", "test")
        self.state.export(self.args.report)
        self.assertIn("Retry", self.args.report.read_text(encoding="utf-8-sig"))
        self.assertIsNotNone(self.state.next())

    def test_loopback_lock_rejects_second_runner(self):
        with x.endpoint_lock("http://localhost:31479"):
            with self.assertRaises((RuntimeError, OSError)):
                with x.endpoint_lock("http://127.0.0.1:31479"):
                    self.fail("Second runner acquired the same endpoint")

    def test_status_does_not_contact_api_or_modify_jobs(self):
        before = self.state.counts()
        self.assertEqual(x.main(["--status", "--state", str(self.path / "state.sqlite")]), 0)
        self.assertEqual(self.state.counts(), before)

    def test_live_rate_status_updates_countdown_without_network_or_queue_changes(self):
        before = self.state.counts()
        self.state.meta('cooldown_until', self.t + 120)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.runner.status('rate_limit', until=self.t + 120)
            self.runner.sleep(60)
        text = out.getvalue()
        self.assertIn('RATENLIMIT', text)
        self.assertIn('Weiter in 2 Min. 00 Sek.', text)
        self.assertIn('Weiter in 1 Min. 00 Sek.', text)
        self.assertEqual(text.count('[STATUS]'), 3)
        self.assertEqual(self.state.counts(), before)
        self.assertEqual(float(self.state.meta('cooldown_until')), 10120)
        self.api.account.assert_not_called()
        self.assertEqual(json.loads(self.state.meta('runtime_status'))['updated'], 10060)

    def test_budget_status_reports_exact_next_restart_and_recovers_after_expiry(self):
        self.state.meta('restarts', json.dumps([6500, 6600, 6700]))
        self.assertFalse(self.runner.try_restart('test', self.restart))
        saved = json.loads(self.state.meta('runtime_status'))
        self.assertEqual(saved['mode'], 'restart_budget')
        self.assertEqual(saved['until'], 10100)
        self.restart.assert_not_called()
        self.t = 10101
        self.assertTrue(self.runner.try_restart('test', self.restart))
        self.restart.assert_called_once()

    def test_minimum_restart_spacing_and_disabled_budget_are_distinct(self):
        self.state.meta('restarts', json.dumps([9990]))
        self.assertFalse(self.runner.try_restart('test', self.restart))
        saved = json.loads(self.state.meta('runtime_status'))
        self.assertEqual(saved['mode'], 'restart_spacing')
        self.assertEqual(saved['until'], 10290)
        self.args.max_restarts = 0
        self.assertFalse(self.runner.try_restart('test', self.restart))
        self.assertEqual(json.loads(self.state.meta('runtime_status'))['mode'], 'restart_disabled')
        self.restart.assert_not_called()

    def test_rate_limit_has_display_priority_over_restart_budget(self):
        self.state.meta('cooldown_until', 10120)
        self.runner.status('restart_budget', '3/3 Neustarts', until=11000)
        saved = json.loads(self.state.meta('runtime_status'))
        self.assertEqual(saved['mode'], 'rate_limit')
        self.assertEqual(saved['until'], 10120)
        text = x.format_runtime_status(saved, self.state.counts(), self.t, 10120)
        self.assertIn('RATENLIMIT', text)
        self.assertNotIn('NEUSTARTBUDGET', text)

    def test_terminal_status_is_not_overridden_by_old_cooldown(self):
        self.state.meta('cooldown_until', 10120)
        self.runner.status('stopped', 'Kontowechsel')
        saved = json.loads(self.state.meta('runtime_status'))
        self.assertEqual(saved['mode'], 'stopped')
        text = x.format_runtime_status(saved, self.state.counts(), self.t, 10120)
        self.assertIn('GESTOPPT', text)
        self.assertNotIn('RATENLIMIT', text)

    def test_authentication_wait_displays_session_status(self):
        self.args.health_wait = 20
        self.api.account.side_effect = x.ApiError('authentication', 401)
        with self.assertRaises(RuntimeError):
            self.runner.wait_health()
        self.assertEqual(json.loads(self.state.meta('runtime_status'))['mode'], 'session')
        self.api.unlock.assert_not_called()

    def test_saved_status_is_read_only_and_labels_snapshot_age(self):
        self.runner.status('processing', 'Example (ID 123)')
        before = self.state.meta('runtime_status')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            x.show_status(self.path / 'state.sqlite')
        self.assertIn('VERARBEITUNG', out.getvalue())
        self.assertIn('keine Live-Prozesspruefung', out.getvalue())
        self.assertEqual(self.state.meta('runtime_status'), before)

    def test_status_without_database_does_not_create_it(self):
        missing = self.path / "missing.sqlite"
        self.assertEqual(x.main(["--status", "--state", str(missing)]), 0)
        self.assertFalse(missing.exists())

    def test_status_shows_remaining_rate_limit_wait(self):
        self.state.meta("cooldown_until", x.time.time() + 125)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            x.show_status(self.path / "state.sqlite")
        self.assertIn("Xbox-Ratenlimit: Pause noch 2 Min.", out.getvalue())
        self.assertIn("Kein Neustart erforderlich", out.getvalue())

    def test_status_does_not_show_expired_pause(self):
        self.state.meta("cooldown_until", x.time.time() - 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            x.show_status(self.path / "state.sqlite")
        self.assertNotIn("Xbox-Ratenlimit: Pause", out.getvalue())


class HttpTests(unittest.TestCase):
    def test_real_http_retry_after_and_profile_shape(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/api/profile/me":
                    code, data = 200, {"People": [{"Xuid": "1111"}]}
                else:
                    code, data = 500, {"upstreamStatus": 429, "retryAfterSeconds": 70, "error": "limited"}
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "60")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = x.Api(f"http://127.0.0.1:{server.server_port}")
            self.assertEqual(api.account(), "1111")
            with self.assertRaises(x.ApiError) as result:
                api.achievements("123")
            self.assertEqual(result.exception.kind, "rate_limit")
            self.assertEqual(result.exception.retry_after, 70)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
