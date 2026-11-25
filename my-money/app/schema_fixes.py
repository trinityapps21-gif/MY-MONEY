"""One-off fixes to the LIVE database that `flask sync-schema` cannot make by adding a column.

`import_batches` was created with a CHECK constraint that only allowed the first four kinds of import
(members, contributions, loans, repayments), so saving a Shares / Income / Expenses sheet failed with
"CHECK constraint failed: ck_import_kind". SQLite cannot drop a constraint in place, so the table is rebuilt
without it (the app validates the kind itself). Safe to run on every start: it does nothing once the
constraint is gone. Pure SQL on the connection it is given, so it works for SQLite, libSQL/Turso and tests."""
import re

_KIND_CHECK = re.compile(r",?\s*CONSTRAINT\s+\"?ck_import_kind\"?\s+CHECK\s*\((?:[^()]|\([^()]*\))*\)", re.I)


def drop_import_kind_check(conn) -> bool:
    """Rebuild import_batches without ck_import_kind, keeping every row, id and index. True if it changed anything.

    One transaction, so any failure leaves the table exactly as it was. The rows are copied aside, the table is
    dropped and created again under the same name, and the rows are put back. `defer_foreign_keys` postpones the
    foreign-key check to the commit, by which time every parent row is back, so the import_items that point at
    import_batches stay valid (tested on real SQLite with foreign keys switched on)."""
    ddl = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='import_batches'").scalar()
    if not ddl or "ck_import_kind" not in ddl:
        return False
    new_ddl = _KIND_CHECK.sub("", ddl, count=1)
    if "ck_import_kind" in new_ddl or not new_ddl.lstrip().upper().startswith("CREATE TABLE"):
        raise RuntimeError("Could not rewrite the import_batches definition safely; nothing was changed.")
    indexes = [r[0] for r in conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='import_batches' AND sql IS NOT NULL")]
    before = conn.exec_driver_sql("SELECT COUNT(*) FROM import_batches").scalar()
    conn.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    conn.exec_driver_sql("DROP TABLE IF EXISTS import_batches__bak")
    conn.exec_driver_sql("CREATE TABLE import_batches__bak AS SELECT * FROM import_batches")
    conn.exec_driver_sql("DROP TABLE import_batches")
    conn.exec_driver_sql(new_ddl)
    conn.exec_driver_sql("INSERT INTO import_batches SELECT * FROM import_batches__bak")
    if conn.exec_driver_sql("SELECT COUNT(*) FROM import_batches").scalar() != before:
        raise RuntimeError("Row count changed while rebuilding import_batches; rolled back.")
    conn.exec_driver_sql("DROP TABLE import_batches__bak")
    for sql in indexes:
        conn.exec_driver_sql(sql)
    return True
