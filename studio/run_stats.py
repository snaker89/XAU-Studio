"""Per-run evidence for newly completed titles; never inferred from Verified totals."""
import json
import time
import uuid


class RunStats:
    def __init__(self, state):
        self.state = state
        self.id = uuid.uuid4().hex
        self.started = time.time()
        with state.db:
            state.db.execute('''CREATE TABLE IF NOT EXISTS studio_run_titles (
                run_id TEXT NOT NULL, title TEXT NOT NULL, locked_ids TEXT NOT NULL,
                write_attempted INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0, new_achievements INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, title))''')
            self.publish()

    def publish(self):
        count = self.state.db.execute(
            'SELECT COUNT(*) FROM studio_run_titles WHERE run_id=? AND completed=1',
            (self.id,)).fetchone()[0]
        self.state.db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)',
                             ('studio_run_stats', json.dumps({'run_id': self.id,
                              'started': self.started, 'newly_completed': count})))
        return count

    def before_unlock(self, title):
        with self.state.db:
            self.state.db.execute('UPDATE studio_run_titles SET write_attempted=1 WHERE run_id=? AND title=?',
                                  (self.id, title))

    def observe(self, title, data, result):
        if result[0] == 'Unsupported':
            return
        items = data['achievements']
        locked = {str(a['id']) for a in items if a['progressState'] != 'Achieved'}
        achieved = {str(a['id']) for a in items if a['progressState'] == 'Achieved'}
        newly_counted = False
        with self.state.db:
            # Preserve the first validated read across every retry in this run.
            self.state.db.execute('INSERT OR IGNORE INTO studio_run_titles(run_id,title,locked_ids) VALUES (?,?,?)',
                                  (self.id, title, json.dumps(sorted(locked))))
            row = self.state.db.execute('SELECT locked_ids,write_attempted,completed FROM studio_run_titles WHERE run_id=? AND title=?',
                                        (self.id, title)).fetchone()
            baseline = set(json.loads(row[0]))
            if result[0] == 'Verified' and baseline and baseline.issubset(achieved) and row[1] and not row[2]:
                self.state.db.execute('UPDATE studio_run_titles SET completed=1,new_achievements=? WHERE run_id=? AND title=?',
                                      (len(baseline), self.id, title))
                count = self.publish()
                newly_counted = True
        if newly_counted:
            self.state.event(title, 'NewlyCompleted',
                             f'New achievements verified: {len(baseline)}; newly completed games in this run: {count}')
