from datetime import datetime, timedelta, timezone

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


def test_migration_comments_may_contain_semicolons(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    extra = tmp_path / "migrations"
    extra.mkdir()
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        (extra / f.name).write_text(f.read_text())
    (extra / "998_note.sql").write_text("-- Deux tables ; la seconde garde l'historique.\nCREATE TABLE t1 (x INTEGER);\n"
                                        "  -- fin ; voilà\nCREATE TABLE t2 (y INTEGER);\n")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", extra)
    db.migrate(conn, tmp_path / "backups")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 998
    assert conn.execute("SELECT COUNT(*) FROM t2").fetchone()[0] == 0

def test_migration_024_resets_stale_redemption_dates(tmp_path):
    """Début de rédemption pris sur une vieille modification RDAP : libération estimée déjà passée.
    Ces domaines repassent en première vérification, les autres ne bougent pas."""
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    conn.execute(
        "INSERT INTO domains (name, added_at, state, state_since, last_checked_at, next_check_at) VALUES "
        "('vieux.com', 'x', 'redemption', '2024-01-05T12:00:00+00:00', '2026-10-12T08:00:00+00:00', '2026-10-12T09:00:00+00:00'), "
        "('recent.com', 'x', 'redemption', ?, '2026-10-12T08:00:00+00:00', '2026-10-12T09:00:00+00:00')",
        ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),),
    )
    conn.execute("PRAGMA user_version = 23")
    db.migrate(conn, tmp_path / "backups")
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM domains")}
    assert rows["vieux.com"]["state"] is None and rows["vieux.com"]["next_check_at"] == "2026-10-12T08:00:00+00:00"
    assert rows["recent.com"]["state"] == "redemption"


def test_com_opinions_go_back_to_the_rating_queue(tmp_path):
    """Consigne corrigée sur les .com : leurs avis repassent dans la file, les autres ne bougent pas."""
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    for name in ("motfrancais.com", "velo.fr"):
        conn.execute("INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, "
                     "rated_at, version) VALUES (?, 40, 40, 0, 100, 500, 2000, 'À laisser. X. Acheteur type : y.', 'x', 5)",
                     (name,))
    conn.execute("PRAGMA user_version = 34")
    db.migrate(conn, tmp_path / "backups")
    versions = dict(conn.execute("SELECT name, version FROM domain_ai").fetchall())
    assert versions == {"motfrancais.com": 4, "velo.fr": 5}


def test_provisional_com_data_is_removed(tmp_path):
    """Accès CZDS au .com accordé : lignes .com du DNS et réglages du mécanisme provisoire effacés ; .co, .ai, .io et
    les autres réglages gardés. Rejouée, la migration ne change plus rien."""
    import json
    import re

    sql = (db.MIGRATIONS_DIR / "042_retrait_com_provisoire.sql").read_text()
    retired = set(re.findall(r"'(\w+)'", sql.split("DELETE FROM settings")[1].split(";")[0]))
    assert len(retired) == 11 and {"com_dns_status", "com_dns_last", "gandi_recheck"} <= retired
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    at = "2026-10-19T03:00:00+00:00"
    for name in ("maison.com", "menu.co", "chat.ai"):
        conn.execute("INSERT INTO cc_dns (name, present, checked_at) VALUES (?, 1, ?)", (name, at))
    kept = {"co_dns_status": "{}", "gandi_pass_rate": "3", "gandi_pat": "pat_test"}
    for key, value in [*((k, "1") for k in retired), *kept.items(),
                       ("gandi_unsold", json.dumps({"com": "2099-01-01", "xyz": "2099-01-01"}))]:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    for _ in range(2):
        conn.execute("PRAGMA user_version = 41")
        db.migrate(conn, tmp_path / "backups")
        assert [r[0] for r in conn.execute("SELECT name FROM cc_dns ORDER BY name")] == ["chat.ai", "menu.co"]
        settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        assert not retired & set(settings) and kept.items() <= settings.items()
        assert json.loads(settings["gandi_unsold"]) == {"xyz": "2099-01-01"}
