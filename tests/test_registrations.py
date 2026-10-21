import asyncio
from datetime import date, datetime, timedelta, timezone

from app import ai_format, registrations, stats
from app.checker import utcnow
from tests.test_app import client_factory, logged  # noqa: F401  mêmes fixtures que l'appli
from tests.test_explore import env  # noqa: F401
from tests.test_frdata import csv_text, fr, make_zip, serve  # noqa: F401


def test_zone_journal_skips_names_of_a_list_added_since_the_last_read(env):
    env.conn.execute("INSERT INTO drops (name, tld, label, kind, removed_on) VALUES ('velo.com', 'com', 'velo', 'word', '2026-09-01')")
    now = datetime(2026, 10, 21, 2, tzinfo=timezone.utc)
    kept = registrations.journal_zone(env.conn, "com", ["velo", "maison", "nouveau"], {"velo", "maison"}, ["abcd"], now)
    assert kept == 3  # « nouveau » vient d'une liste ajoutée : déjà enregistré, pas un enregistrement du jour
    rows = {r["name"]: r for r in env.conn.execute("SELECT * FROM registrations")}
    assert set(rows) == {"velo.com", "maison.com", "abcd.com"}
    assert rows["velo.com"]["removed_on"] == "2026-09-01" and rows["maison.com"]["removed_on"] is None
    assert rows["velo.com"]["day"] == "2026-10-21" and rows["velo.com"]["source"] == "zone"
    # Lecture d'avant ce suivi (noms cherchés inconnus) : seuls les noms courts comptent.
    assert registrations.journal_zone(env.conn, "org", ["velo"], None, [], now) == 0
    # Deux fois la même lecture : une seule ligne.
    registrations.journal_zone(env.conn, "com", ["velo"], {"velo"}, [], now)
    assert env.conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 3


def test_context_is_added_after_the_fact_with_what_was_known_then(env):
    now = utcnow()
    registrations.journal_zone(env.conn, "com", ["maison", "velo"], {"maison", "velo"}, [], now)
    item = {"name": "maison.com", "oral": 9, "radio": 9, "memo": 7, "sens": 9, "marque": 6, "marche": 8, "ext": 10,
            "acheteurs": 7, "risque": 0, "comment": "Mot courant. Acheteur type : agence."}
    item.update(ai_format.default_prices(item))
    from app import ai
    ai.save(env.conn, [item], lambda n: True)
    env.conn.execute("UPDATE domain_ai SET rated_at = ?", ((now - timedelta(days=3)).isoformat(),))
    assert registrations.enrich(env) == 2 and registrations.enrich(env) == 0
    rows = {r["name"]: r for r in env.conn.execute("SELECT * FROM registrations")}
    assert rows["maison.com"]["ai_verdict"] == "À prendre"  # score : vide ici, la zone .com n'est pas lue
    assert "word" in rows["maison.com"]["categories"] and rows["velo.com"]["ai_verdict"] is None


def test_afnic_creations_fill_the_journal_once_then_only_new_dates(fr):
    today = datetime.now(timezone.utc).date()
    recent = (today - timedelta(days=20)).strftime("%d-%m-%Y")
    archive = make_zip(csv_text([
        f"navarro.fr;FR;;;;;PP;FR;;{recent};",
        "velo.fr;FR;;;;;PP;FR;;01-02-2010;",
        f"courir.fr;FR;;;;;PP;FR;;{recent};{recent}",
    ]))
    serve(fr, archive)
    asyncio.run(fr.import_file())
    rows = [tuple(r) for r in fr.conn.execute("SELECT name, day, source FROM registrations")]
    # Créé il y a 20 jours et toujours enregistré : dans le journal ; velo (2010) trop ancien, courir supprimé.
    assert rows == [("navarro.fr", (today - timedelta(days=20)).isoformat(), "afnic")]


def test_afnic_window_and_previous_removal(env):
    today = date(2026, 10, 21)
    rows = [("velo", date(2026, 10, 1), date(2026, 9, 2)), ("vieux", date(2024, 1, 1), None)]
    assert registrations.journal_afnic(env.conn, rows, today) == 1
    row = env.conn.execute("SELECT * FROM registrations").fetchone()
    assert (row["name"], row["removed_on"]) == ("velo.fr", "2026-09-02")


def test_stats_page_reads_every_block(logged):
    client = logged()
    conn = client.app.state.conn
    now = utcnow()
    day = now.date().isoformat()
    registrations.journal_zone(conn, "com", ["maison"], {"maison"}, ["abcd"], now)
    conn.execute("INSERT INTO registrations (name, tld, label, day, source, removed_on) VALUES ('velo.fr', 'fr', 'velo', ?, 'afnic', ?)",
                 (day, (now.date() - timedelta(days=12)).isoformat()))
    old = (now.date() - timedelta(days=20)).isoformat()
    for k in range(40):
        conn.execute("INSERT INTO drops (name, tld, label, kind, removed_on, state, state_since, lists) VALUES (?, 'com', ?, 'word', ?, ?, ?, 'word')",
                     (f"n{k}.com", f"n{k}", old, "taken" if k < 10 else None, now.isoformat() if k < 10 else None))
    stats.clear()
    found = client.get("/api/stats").json()
    reg = found["registrations"]
    assert reg["total"] == 3 and reg["total_7"] == 3 and sum(reg["by_day"]["com"]) == 2 and len(reg["days"]) == stats.DAYS
    assert reg["retaken"]["fr"] == {"count": 1, "median_days": 12}
    retake = found["releases"]["by_tld"]
    assert retake == [{"key": "com", "label": "com", "count": 40, "taken": 10, "share": 0.25, "median_days": 20}]
    assert found["releases"]["by_category"][0]["label"] == "Mots"
    assert set(found["ai"]) >= {"per_day", "verdicts", "escalation", "panel", "outcomes"} and "per_day" in found["gandi"]
    # Lecture seule avec le jeton de lecture : la page fait partie des lectures ouvertes.
    token = client.post("/api/settings/ai-token").json()["token"]
    client.cookies.clear()
    assert client.get("/api/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    client.__exit__(None, None, None)
