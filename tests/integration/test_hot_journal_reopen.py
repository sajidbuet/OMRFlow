"""A project reopens after a writer was killed mid-commit (hot journal).

Found by revised phase 5's real-kill intake test: a process killed inside a
commit leaves ``database.sqlite-journal``. ``open_project`` first peeks at the
schema version through a **read-only** connection (to decide on a
pre-migration backup); SQLite cannot roll a hot journal back on such a
connection, and the resulting "attempt to write a readonly database" escaped,
so the project could not be opened at all. The writer here is a real process,
killed with ``kill()`` after its uncommitted pages have spilled to the file.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap

from omr_scanner.services import create_project, open_project


def test_open_after_a_kill_mid_commit_recovers_the_last_committed_state(tmp_path):
    created = create_project(tmp_path, "hot")
    root, database_path = created.root, created.database.path
    created.close()
    script = textwrap.dedent(
        f"""
        import sqlite3, time
        connection = sqlite3.connect(r'{database_path}', isolation_level=None)
        connection.execute('PRAGMA cache_size=1')
        connection.execute('BEGIN')
        connection.execute('CREATE TABLE uncommitted(x)')
        for _ in range(20000):
            connection.execute('INSERT INTO uncommitted VALUES (randomblob(200))')
        print('spilled', flush=True)
        time.sleep(120)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None and child.stdout.readline().strip() == "spilled"
    child.kill()
    child.wait(timeout=30)
    journal = database_path.with_name(database_path.name + "-journal")
    assert journal.exists(), "the kill must leave a hot journal for this test to mean anything"

    with open_project(root) as session:  # raised OperationalError before the fix
        assert session.database.schema_version > 0
    assert not journal.exists()
    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "uncommitted" not in tables
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with open_project(root, read_only=True) as session:
        assert session.database.schema_version > 0
