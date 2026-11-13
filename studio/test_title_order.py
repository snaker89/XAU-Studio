from pathlib import Path
import tempfile
import time
import unittest
import worker


class TitleOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = worker.StudioState(Path(self.temp.name) / 'state.sqlite', random_titles=True)
        self.state.seed([('1', 'First'), ('2', 'Second'), ('3', 'Third')])

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def test_random_selection_can_choose_later_title(self):
        values = iter([30, 10, 20])
        self.state.db.create_function('random', 0, lambda: next(values))
        self.assertEqual(self.state.next()['id'], '2')

    def test_disabled_preserves_list_order(self):
        self.state.random_titles = False
        self.assertEqual(self.state.next()['id'], '1')

    def test_finished_titles_never_return_and_no_title_is_lost(self):
        seen = set()
        while job := self.state.next():
            self.assertNotIn(job['id'], seen)
            seen.add(job['id'])
            self.state.save(job['id'], 'Verified')
        self.assertEqual(seen, {'1', '2', '3'})

    def test_failed_and_unsupported_are_excluded(self):
        self.state.save('1', 'Failed')
        self.state.save('2', 'Unsupported')
        self.state.save('3', 'NeedsAttention')
        self.assertIsNone(self.state.next())

    def test_future_retry_does_not_block_ready_title(self):
        self.state.save('1', 'Retry', due=time.time() + 300)
        self.state.save('2', 'Retry', due=time.time() + 60)
        self.assertEqual(self.state.next()['id'], '3')
        self.state.save('3', 'Verified')
        self.assertEqual(self.state.next()['id'], '2')
        self.assertGreater(self.state.next()['due'], time.time())

    def test_interrupted_title_is_verified_before_random_next(self):
        self.state.save('3', 'InFlight')
        self.assertEqual(self.state.next()['id'], '3')

    def test_schema_repair_only_reopens_old_recoverable_400_once(self):
        self.state.save('1', 'Retry', 'rejected (HTTP 400)', due=time.time()+100, attempts=3)
        self.state.save('2', 'NeedsAttention', 'rejected (HTTP 400)', attempts=4)
        self.state.save('3', 'Failed', 'rejected (HTTP 400)', attempts=4)
        self.state.meta('cooldown_until', 12345678999)
        self.assertEqual(self.state.repair_update_schema_failures(), 2)
        rows = list(self.state.db.execute('SELECT status,attempts,due FROM jobs ORDER BY rowid'))
        self.assertEqual([tuple(r) for r in rows], [('Pending', 0, 0), ('Pending', 0, 0), ('Failed', 4, 0)])
        self.assertEqual(self.state.meta('cooldown_until'), '12345678999')
        self.state.save('1', 'Retry', 'rejected (HTTP 400)', attempts=1)
        self.assertEqual(self.state.repair_update_schema_failures(), 0)

    def test_schema_repair_keeps_other_errors(self):
        self.state.save('1', 'Retry', 'rate_limit (HTTP 429)', attempts=1)
        self.state.save('2', 'NeedsAttention', 'no_data (HTTP 404)', attempts=4)
        self.state.save('3', 'Verified', 'All returned achievements are Achieved')
        self.assertEqual(self.state.repair_update_schema_failures(), 0)


if __name__ == '__main__':
    unittest.main()
