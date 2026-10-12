from app import db


def test_migrations_and_backup_rotation(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    backups = tmp_path / "backups"
    conn = db.connect(path)
    db.migrate(conn, backups)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 1
    assert not backups.exists()

    backups.mkdir()
    for i in range(12):
        (backups / f"domain-watch-20200101-0000{i:02d}.db").write_bytes(b"")

    extra = tmp_path / "migrations"
    extra.mkdir()
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        (extra / f.name).write_text(f.read_text())
    (extra / "999_test.sql").write_text("CREATE TABLE t (x INTEGER);")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", extra)
    conn.execute("INSERT INTO domains (name, added_at) VALUES ('a.fr', 'x')")
    db.migrate(conn, backups)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 999
    kept = sorted(backups.glob("domain-watch-*.db"))
    assert len(kept) == 10
    newest = db.connect(kept[-1])
    assert newest.execute("SELECT name FROM domains").fetchone()[0] == "a.fr"
