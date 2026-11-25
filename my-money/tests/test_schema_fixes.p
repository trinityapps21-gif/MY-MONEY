"""The live-database fix that lets Shares / Income / Expenses sheets be saved. Plain sqlite3: needs no app or Flask."""
import sqlite3

from app.schema_fixes import drop_import_kind_check

OLD = """CREATE TABLE import_batches (
	id INTEGER NOT NULL,
	group_id INTEGER NOT NULL,
	kind VARCHAR(20) NOT NULL,
	filename VARCHAR(200) NOT NULL,
	status VARCHAR(12) NOT NULL,
	bundle VARCHAR(24),
	rows_json TEXT,
	PRIMARY KEY (id),
	CONSTRAINT ck_import_kind CHECK (kind IN ('members','contributions','loans','repayments')),
	CONSTRAINT ck_import_status CHECK (status IN ('PREVIEW','APPLIED','UNDONE','CANCELLED'))
)"""


class Conn:
    """Just enough of a SQLAlchemy connection for the helper."""
    def __init__(self, db):
        self.db = db

    class _Result:
        def __init__(self, cur):
            self.cur = cur

        def scalar(self):
            return (self.cur.fetchone() or [None])[0]

        def __iter__(self):
            return iter(self.cur.fetchall())

    def exec_driver_sql(self, sql):
        return self._Result(self.db.execute(sql))


def _db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(OLD)
    db.execute("CREATE INDEX ix_import_batches_group_id ON import_batches (group_id)")
    db.execute("CREATE INDEX ix_import_batches_bundle ON import_batches (bundle)")
    db.execute("CREATE TABLE import_items (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, "
               "FOREIGN KEY(batch_id) REFERENCES import_batches (id))")
    db.execute("INSERT INTO import_batches VALUES (1, 3, 'members', 'a.csv', 'APPLIED', NULL, '[]')")
    db.execute("INSERT INTO import_batches VALUES (2, 3, 'loans', 'b.csv', 'PREVIEW', 'abc', '[1]')")
    db.execute("INSERT INTO import_items VALUES (10, 1), (11, 1), (12, 2)")
    return db


def _fix(db):
    db.execute("BEGIN")
    try:
        changed = drop_import_kind_check(Conn(db))
        db.execute("COMMIT")
        return changed
    except Exception:
        db.execute("ROLLBACK")
        raise


def test_old_table_rejects_new_kinds_and_fix_allows_them_keeping_everything():
    db = _db()
    try:
        db.execute("INSERT INTO import_batches (group_id, kind, filename, status) VALUES (3, 'shares', 'x', 'PREVIEW')")
        assert False, "the old constraint should refuse 'shares'"
    except sqlite3.IntegrityError:
        pass
    assert _fix(db) is True
    for kind in ("shares", "income", "expenses"):
        db.execute("INSERT INTO import_batches (group_id, kind, filename, status) VALUES (3, ?, 'x', 'PREVIEW')", (kind,))
    rows = db.execute("SELECT id, kind, status, bundle, rows_json FROM import_batches WHERE id <= 2 ORDER BY id").fetchall()
    assert rows == [(1, "members", "APPLIED", None, "[]"), (2, "loans", "PREVIEW", "abc", "[1]")]   # nothing lost
    assert db.execute("SELECT COUNT(*) FROM import_items").fetchone()[0] == 3                        # children untouched
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []                                   # and still valid
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='import_batches'")}
    assert {"ix_import_batches_group_id", "ix_import_batches_bundle"} <= names                       # indexes rebuilt
    try:
        db.execute("INSERT INTO import_batches (group_id, kind, filename, status) VALUES (3, 'x', 'x', 'BOGUS')")
        assert False, "the status constraint must stay"
    except sqlite3.IntegrityError:
        pass


def test_fix_is_idempotent_and_new_ids_continue():
    db = _db()
    assert _fix(db) is True
    assert _fix(db) is False                                    # second run (every restart) changes nothing
    db.execute("INSERT INTO import_batches (group_id, kind, filename, status) VALUES (3, 'shares', 'x', 'PREVIEW')")
    assert db.execute("SELECT MAX(id) FROM import_batches").fetchone()[0] == 3
