import json
from pathlib import Path
import tempfile
import unittest

import worker
from run_stats import RunStats


class RunStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = worker.queue.State(Path(self.temp.name) / 'state.sqlite')
        self.stats = RunStats(self.state)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def observe(self, locked=(), achieved=(), complete=False):
        items = [{'id': a, 'progressState': 'NotStarted'} for a in locked]
        items += [{'id': a, 'progressState': 'Achieved'} for a in achieved]
        self.stats.observe('7', {'achievements': items}, ('Verified' if complete else 'Pending', len(items), ''))

    def count(self):
        return json.loads(self.state.meta('studio_run_stats'))['newly_completed']

    def test_previously_complete_does_not_count(self):
        self.observe(achieved=['a'], complete=True)
        self.assertEqual(self.count(), 0)

    def test_partial_progress_and_write_receipt_do_not_count(self):
        self.observe(locked=['a', 'b'])
        self.stats.before_unlock('7')
        self.assertEqual(self.count(), 0)
        self.observe(locked=['b'], achieved=['a'])
        self.assertEqual(self.count(), 0)

    def test_completion_after_retry_counts_once(self):
        self.observe(locked=['a', 'b'], achieved=['old'])
        self.stats.before_unlock('7')
        self.observe(locked=['b'], achieved=['old', 'a'])
        self.stats.before_unlock('7')
        self.observe(achieved=['old', 'a', 'b'], complete=True)
        self.observe(achieved=['old', 'a', 'b'], complete=True)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.state.db.execute('SELECT new_achievements FROM studio_run_titles').fetchone()[0], 2)

    def test_missing_previously_locked_ids_do_not_count(self):
        self.observe(locked=['a', 'b'])
        self.stats.before_unlock('7')
        self.observe(achieved=['a'], complete=True)
        self.assertEqual(self.count(), 0)

    def test_completion_without_own_update_does_not_count(self):
        self.observe(locked=['a'])
        self.observe(achieved=['a'], complete=True)
        self.assertEqual(self.count(), 0)

    def test_new_run_resets_and_keeps_history(self):
        self.observe(locked=['a'])
        self.stats.before_unlock('7')
        self.observe(achieved=['a'], complete=True)
        previous = self.stats.id
        self.stats = RunStats(self.state)
        self.assertEqual(self.count(), 0)
        self.assertNotEqual(self.stats.id, previous)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM studio_run_titles WHERE run_id=? AND completed=1', (previous,)).fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
