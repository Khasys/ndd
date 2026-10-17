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


def test_each_thread_has_its_own_connection(tmp_path):
    """Boucle principale, fil de l'Explorer, run_in_executor : jamais la même connexion en même temps."""
    import threading

    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    conn.execute("INSERT INTO domains (name, added_at) VALUES ('a.fr', 'x')")
    seen, errors = {}, []

    def work(n):
        try:
            seen[n] = id(conn._conn())
            # Écrit par un autre fil : visible ici.
            assert conn.execute("SELECT COUNT(*) FROM domains WHERE name = 'a.fr'").fetchone()[0] == 1
            for i in range(200):
                conn.execute("INSERT INTO domains (name, added_at) VALUES (?, 'x')", (f"t{n}-{i}.fr",))
                list(conn.execute("SELECT name FROM domains ORDER BY name LIMIT 50"))
        except Exception as e:  # pragma: no cover - échec du test
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(seen.values()) | {id(conn._conn())}) == 5
    assert conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 801
