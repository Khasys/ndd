"""Pépites et alertes : qui est une pépite (Veille et Explorer, règle unique des états), l'alerte immédiate (une
fois par nom et par événement, regroupée, jamais en double), le quotidien (sans répétition), les réglages."""

import json
from datetime import datetime, timedelta, timezone

from app import db, explore, morning, notify, pepites
from app.checker import iso
from app.store import Store
from tests.test_app import client_factory, logged  # noqa: F401  (fixtures)
from tests.test_explore import add_fr, env  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)


def _opinion(conn, name, resale=85, verdict="À prendre", rated_at="2026-10-01T00:00:00+00:00", final=(3000, 8000)):
    conn.execute(
        "INSERT OR REPLACE INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, "
        "rated_at, version) VALUES (?, 70, ?, 500, 1000, ?, ?, ?, ?, 5)",
        (name, resale, final[0], final[1], f"{verdict}. Mot courant, très demandé. Acheteur type : enseigne.", rated_at))


def _gandi(conn, name, kind, price=None, at=NOW):
    conn.execute("INSERT OR REPLACE INTO gandi_checks (name, status, kind, price, price_ht, currency, checked_at) "
                 "VALUES (?, ?, ?, ?, ?, 'EUR', ?)", (name, kind, kind, price, price, iso(at)))


def _drop(ex, name, state, at=NOW, release=None):
    label, tld = name.split(".")
    ex.drops.record(tld, [label], [], at)
    ex.conn.execute("UPDATE drops SET state = ?, state_since = ?, available_since = ?, release_at = ?, checked_at = ? "
                    "WHERE name = ?", (state, iso(at), iso(at) if state == "available" else None,
                                       iso(release) if release else None, iso(at), name))


def _setup(ex):
    """Un .fr lu (sel.fr enregistré), des noms de la Veille et des noms jamais enregistrés (Explorer)."""
    add_fr(ex, "sel")
    _drop(ex, "velo.fr", "available")                                   # libéré, vérifié au registre
    _drop(ex, "mer.fr", "unconfirmed")                                  # libre selon le seul WHOIS
    _drop(ex, "lune.fr", "redemption", release=NOW + timedelta(days=20))  # bientôt libre
    _gandi(ex.conn, "chat.fr", "available", 9)                          # jamais enregistré, libre chez Gandi
    _gandi(ex.conn, "pain.fr", "unavailable")                           # jamais enregistré, indisponible à l'achat
    for name in ("velo.fr", "mer.fr", "lune.fr", "chat.fr", "pain.fr", "ciel.fr", "sel.fr", "rose.fr"):
        _opinion(ex.conn, name)
    _opinion(ex.conn, "nuit.fr", resale=60)                             # sous le seuil
    _gandi(ex.conn, "nuit.fr", "available", 9)
    _gandi(ex.conn, "rose.fr", "available", 9)
    ex.conn.execute("INSERT INTO drop_hidden (name, hidden_at) VALUES ('rose.fr', 'x')")  # ignoré
    ex.invalidate("fr")


def test_pepites_cover_veille_and_explorer_names_by_the_single_state_rule(env):
    _setup(env)
    found = pepites.scan(env, NOW, 70)
    states_ = {n: p["state"] for n, p in found.items()}
    # ciel.fr : probablement libre, rien de vérifié ; pain.fr : indisponible chez Gandi ; sel.fr : enregistré.
    assert states_ == {"velo.fr": "free", "mer.fr": "unconfirmed", "lune.fr": "coming", "chat.fr": "free",
                       "pain.fr": None, "ciel.fr": None, "sel.fr": "taken"}
    assert found["velo.fr"]["registry"] and not found["chat.fr"]["registry"] and found["chat.fr"]["price"] == 9
    assert found["lune.fr"]["release_at"] == iso(NOW + timedelta(days=20))
    # Même état que l'Explorer, pour chaque nom (règle unique explore.gandi_effect).
    data = env.status_bytes("fr")
    for name, p in found.items():
        assert p["code"] == data[env.universe.index(name.split(".")[0])]
    # Un premium vu chez Gandi s'achète : pépite, au prix affiché ; un premium probable non vérifié n'en est pas une.
    _gandi(env.conn, "ciel.fr", "premium", 878)
    env.touch("ciel.fr")
    p = pepites.scan(env, NOW, 70)["ciel.fr"]
    assert p["state"] == "premium" and p["price"] == 878 and pepites.state_text(p) == "premium chez Gandi"


def test_a_name_gandi_refuses_is_never_alerted_free(env):
    _setup(env)
    pepites.sync(env.conn, pepites.scan(env, NOW, 70), NOW)  # référence posée
    _drop(env, "jour.fr", "available", at=NOW)
    _opinion(env.conn, "jour.fr")
    _gandi(env.conn, "jour.fr", "unavailable", at=NOW + timedelta(minutes=1))  # après la lecture du registre
    env.touch("jour.fr")
    later = NOW + timedelta(minutes=2)
    found = pepites.scan(env, later, 70)
    # Revérification au registre pas encore faite : l'état d'avant tient (libre), mais aucune alerte ne part.
    assert found["jour.fr"]["state"] == "free" and found["jour.fr"]["pending"]
    pepites.sync(env.conn, found, later)
    assert pepites.due_free(env.conn, found, later, set(), False) == ([], [])
    # Registre revu, toujours libre : indisponible à l'achat, plus une pépite, ni dans Aujourd'hui.
    env.conn.execute("UPDATE drops SET checked_at = ? WHERE name = 'jour.fr'", (iso(later),))
    env.touch("jour.fr")
    found = pepites.scan(env, later, 70)
    assert found["jour.fr"]["state"] is None
    pepites.sync(env.conn, found, later)
    assert "jour.fr" not in [p["name"] for p in pepites.today(env.conn, found, later, 24)["free"]]
    assert pepites.due_free(env.conn, found, later, set(), False) == ([], [])


def test_free_alert_once_per_event_grouped_and_never_twice(env):
    store = env.fr.store
    add_fr(env, "sel")
    env.invalidate("fr")
    args = (env, store)
    # Premier passage : la référence est posée, rien ne part pour les pépites déjà là.
    _drop(env, "velo.fr", "available", at=NOW - timedelta(hours=1))
    _opinion(env.conn, "velo.fr", resale=90)
    env.touch("velo.fr")
    assert notify.pepite_tick(*args, NOW, "https://nas.exemple", "https://shop/?d={domain}", True, False) == (None, [])
    # Deux nouvelles pépites dans la même minute : un seul message, la meilleure d'abord.
    _drop(env, "mer.fr", "unconfirmed", at=NOW)
    _gandi(env.conn, "chat.fr", "available", 12)
    for name, resale in (("mer.fr", 80), ("chat.fr", 88)):
        _opinion(env.conn, name, resale=resale)
        env.touch(name)
    text, names = notify.pepite_tick(*args, NOW, "https://nas.exemple", "https://shop/?d={domain}", True, False)
    assert names == ["chat.fr", "mer.fr"] and text.startswith("Pépites libres : 2 noms")
    assert text.index("chat.fr") < text.index("mer.fr") and "velo.fr" not in text
    assert "État : libre, vérifié chez Gandi" in text and "Prix d'achat : 12 € chez Gandi" in text
    assert "État : libre selon le WHOIS, à confirmer chez un registrar" in text
    assert "Avis IA : À prendre, revente 88, exploitation 70, prix final 3 000 à 8 000 €" in text
    assert "Fiche : https://nas.exemple/?fiche=chat.fr" in text and "Réserver : https://shop/?d=chat.fr" in text
    assert "—" not in text
    # Pas encore marquées (message pas parti) : le passage suivant les reprend ; marquées : plus jamais.
    assert notify.pepite_tick(*args, NOW, "", "", True, False)[1] == names
    pepites.mark_alerted(env.conn, names, NOW)
    assert notify.pepite_tick(*args, NOW + timedelta(minutes=1), "", "", True, False) == (None, [])
    # Redémarrage : nouvel Explorer, même base ; rien ne repart.
    fresh = explore.Explorer(env.conn, env.drops, env.fr)
    assert notify.pepite_tick(fresh, store, NOW + timedelta(minutes=2), "", "", True, False) == (None, [])
    # Une seule : son bloc, titre « Pépite libre ».
    _gandi(env.conn, "lait.fr", "available", 9)
    _opinion(env.conn, "lait.fr")
    env.touch("lait.fr")
    text, names = notify.pepite_tick(*args, NOW, "", "", True, False)
    assert names == ["lait.fr"] and text.splitlines()[0] == "Pépite libre : lait.fr"


def test_free_alert_skips_watched_old_and_disabled_and_waits_for_the_price(env):
    store = env.fr.store
    add_fr(env, "sel")
    env.invalidate("fr")
    notify.pepite_tick(env, store, NOW, "", "", True, False)  # référence
    # Suivi dans Domaines : son alerte de changement d'état suffit. Libéré il y a 3 jours, avis ancien : pas une nouvelle.
    env.conn.execute("INSERT INTO domains (name, added_at) VALUES ('velo.fr', 'x')")
    _drop(env, "velo.fr", "available")
    _drop(env, "mer.fr", "available", at=NOW - timedelta(days=3))
    for name in ("velo.fr", "mer.fr"):
        _opinion(env.conn, name)
        env.touch(name)
    assert notify.pepite_tick(env, store, NOW, "", "", True, False) == (None, [])
    assert env.conn.execute("SELECT COUNT(*) FROM pepites WHERE alerted_at IS NOT NULL").fetchone()[0] == 2
    # Libéré à l'instant, Gandi branché, prix pas encore lu : l'alerte attend le prix (5 minutes au plus).
    _drop(env, "lait.fr", "available")
    _opinion(env.conn, "lait.fr")
    env.touch("lait.fr")
    assert notify.pepite_tick(env, store, NOW + timedelta(minutes=1), "", "", True, True) == (None, [])
    _gandi(env.conn, "lait.fr", "available", 8, at=NOW + timedelta(minutes=1))
    env.touch("lait.fr")
    text, names = notify.pepite_tick(env, store, NOW + timedelta(minutes=2), "", "", True, True)
    assert names == ["lait.fr"] and "Prix d'achat : 8 € chez Gandi" in text and "vérifié au registre" in text
    pepites.mark_alerted(env.conn, names, NOW)
    # Type décoché : marquées sans message, rien en rafale quand on le recoche.
    store.set("alert_on_pepite_free", "0")
    _gandi(env.conn, "pain.fr", "available", 9)
    _opinion(env.conn, "pain.fr")
    env.touch("pain.fr")
    assert notify.pepite_tick(env, store, NOW, "", "", True, False) == (None, [])
    store.set("alert_on_pepite_free", "1")
    assert notify.pepite_tick(env, store, NOW, "", "", True, False) == (None, [])


def test_coming_then_free_is_a_new_event_and_taken_names_restart_after_a_week(env):
    store = env.fr.store
    add_fr(env, "sel")
    env.invalidate("fr")
    notify.pepite_tick(env, store, NOW, "", "", True, False)
    _drop(env, "lune.fr", "redemption", release=NOW + timedelta(days=3))
    _opinion(env.conn, "lune.fr")
    env.touch("lune.fr")
    # Bientôt libre : pas d'alerte immédiate (c'est le quotidien).
    assert notify.pepite_tick(env, store, NOW, "", "", True, False) == (None, [])
    later = NOW + timedelta(days=3)
    _drop(env, "lune.fr", "available", at=later)
    env.touch("lune.fr")
    text, names = notify.pepite_tick(env, store, later, "", "", True, False)
    assert names == ["lune.fr"] and "vérifié au registre" in text
    pepites.mark_alerted(env.conn, names, later)
    # Repris : sa ligne attend ; libre de nouveau 2 jours après (lecture contredite) : même événement, pas d'alerte.
    env.conn.execute("UPDATE drops SET state = 'taken' WHERE name = 'lune.fr'")
    env.touch("lune.fr")
    notify.pepite_tick(env, store, later + timedelta(days=1), "", "", True, False)
    assert env.conn.execute("SELECT state FROM pepites WHERE name = 'lune.fr'").fetchone()[0] == "taken"
    _drop(env, "lune.fr", "available", at=later + timedelta(days=2))
    env.touch("lune.fr")
    assert notify.pepite_tick(env, store, later + timedelta(days=2), "", "", True, False) == (None, [])
    # Repris plus de 7 jours, puis relibéré : nouvel événement, nouvelle alerte.
    env.conn.execute("UPDATE drops SET state = 'taken' WHERE name = 'lune.fr'")
    env.touch("lune.fr")
    notify.pepite_tick(env, store, later + timedelta(days=3), "", "", True, False)
    again = later + timedelta(days=60)
    _drop(env, "lune.fr", "available", at=again)
    env.touch("lune.fr")
    assert notify.pepite_tick(env, store, again, "", "", True, False)[1] == ["lune.fr"]


def test_daily_lists_coming_pepites_without_repeating_them(env):
    store = env.fr.store
    add_fr(env, "sel")
    _drop(env, "lune.fr", "redemption", release=NOW + timedelta(days=10))
    _drop(env, "ciel.fr", "pending_delete", release=NOW + timedelta(days=1))
    for name, resale in (("lune.fr", 90), ("ciel.fr", 80)):
        _opinion(env.conn, name, resale=resale)
    env.invalidate("fr")

    def daily(now):
        found = pepites.scan(env, now, 70)
        pepites.sync(env.conn, found, now)
        return pepites.daily_pick(env.conn, found, now)

    picks = daily(NOW)
    assert [(p["name"], note) for p, note in picks] == [("lune.fr", "nouvelle"), ("ciel.fr", "nouvelle")]
    section = pepites.daily_section(picks, "https://nas.exemple")
    assert section.startswith("Pépites bientôt libres : 2") and "État : en rédemption, libération ~" in section
    assert "suppression imminente, libération ~" in section and "Réserver" not in section
    pepites.mark_daily(env.conn, picks, NOW)
    # Le lendemain : rien de neuf (ciel.fr, annoncé sous 48 h, ne revient pas pour ça).
    assert daily(NOW + timedelta(days=1)) == []
    # Libération de lune.fr sous 48 h : rappelée une fois.
    picks = daily(NOW + timedelta(days=8, hours=12))
    assert [(p["name"], note) for p, note in picks] == [("lune.fr", "libération sous 48 h")]
    pepites.mark_daily(env.conn, picks, NOW + timedelta(days=8, hours=12))
    assert daily(NOW + timedelta(days=9)) == []
    # Date estimée revue : dite une fois.
    env.conn.execute("UPDATE drops SET release_at = ? WHERE name = 'ciel.fr'", (iso(NOW + timedelta(days=12)),))
    assert [(p["name"], note) for p, note in daily(NOW + timedelta(days=9))] == [("ciel.fr", "date revue")]


def test_today_pepites_period_and_order(env):
    _setup(env)
    found = pepites.scan(env, NOW, 70)
    pepites.sync(env.conn, found, NOW)
    gems = pepites.today(env.conn, found, NOW, 24)
    # Devenues libres depuis 24 h : velo (constat), mer (WHOIS), chat (Gandi) ; libération de lune dans 20 jours.
    assert {p["name"] for p in gems["free"]} == {"velo.fr", "mer.fr", "chat.fr"} and gems["coming"] == []
    assert [p["name"] for p in pepites.today(env.conn, found, NOW, 30 * 24)["coming"]] == ["lune.fr"]
    # Tri : note de revente, puis prix final.
    _opinion(env.conn, "mer.fr", resale=95)
    _opinion(env.conn, "chat.fr", resale=85, final=(5000, 20000))
    found = pepites.scan(env, NOW, 70)
    assert [p["name"] for p in pepites.today(env.conn, found, NOW, 24)["free"]] == ["mer.fr", "chat.fr", "velo.fr"]
    # Une semaine après : plus « devenues libres sur 24 h », toujours sur 30 jours.
    week = NOW + timedelta(days=7)
    assert pepites.today(env.conn, found, week, 24)["free"] == []
    assert len(pepites.today(env.conn, found, week, 30 * 24)["free"]) == 3
    # Cartes au format de l'Explorer : l'état affiché est celui de la règle unique.
    cards = {i["name"]: i for i in pepites.items(env, found.values(), NOW)}
    assert cards["chat.fr"]["status"] == explore.FREE and cards["chat.fr"]["gandi_state"] == "available"
    assert cards["mer.fr"]["status"] == explore.UNCONFIRMED and cards["lune.fr"]["status"] == explore.COMING
    assert cards["chat.fr"]["pepite"]["state"] == "free" and "score" in cards["chat.fr"]


def test_health_events_reach_the_daily_once(env):
    store = env.fr.store
    assert morning.record_health(store, {"zone:org": "Zone .org en erreur"}, NOW) == 1
    assert morning.record_health(store, {"zone:org": "Zone .org en erreur"}, NOW) == 0  # déjà noté
    morning.note(store, "DNS .fr : passe arrêtée (refus du serveur).", NOW)
    text, count = morning.health_section(store)
    assert text.startswith("Santé :") and "Apparu le " in text and "Zone .org en erreur" in text
    assert "Signalé le " in text and count == 2
    morning.consume_health(store, count)
    # Le lendemain : le problème dure, une ligne ; puis il est réglé, dit une fois.
    assert morning.health_section(store)[0] == "Santé :\nToujours en cours : Zone .org en erreur"
    morning.record_health(store, {}, NOW)
    text, count = morning.health_section(store)
    assert "Réglé le " in text and "Toujours" not in text
    morning.consume_health(store, count)
    assert morning.health_section(store) == (None, 0)


def test_pack_keeps_sections_whole_under_the_telegram_limit():
    parts = ["a" * 2500, "b" * 2500, "c" * 100, "d" * 5000]
    messages = notify.pack(parts)
    assert [len(m) for m in messages][:2] == [2500, 2602] and all(len(m) <= notify.MAX_TEXT for m in messages)
    assert messages[-1].endswith("[tronqué]")


def test_daily_due_once_a_day_from_the_set_hour(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "b")
    store = Store(conn)
    assert not notify.daily_due(store, datetime(2026, 10, 19, 7, 59))
    assert notify.daily_due(store, datetime(2026, 10, 19, 8, 0))
    assert notify.daily_due(store, datetime(2026, 10, 19, 15, 0))  # appli arrêtée à 8 h : rattrapé
    store.set("alert_daily_last", "2026-10-19")
    assert not notify.daily_due(store, datetime(2026, 10, 19, 23, 0))
    store.set("alert_daily_hour", "6")
    assert notify.daily_due(store, datetime(2026, 10, 20, 6, 0))


def test_migration_keeps_the_old_alert_settings(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        if int(f.name.split("_")[0]) < 34:
            (old / f.name).write_text(f.read_text())
    conn = db.connect(tmp_path / "t.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old)
    db.migrate(conn, tmp_path / "b")
    for key, value in {"alert_take_enabled": "0", "alert_take_min": "80", "morning_enabled": "1", "morning_hour": "7",
                       "morning_min": "50", "digest_enabled": "0", "digest_day": "4", "digest_hour": "18",
                       "morning_last_sent": "2026-10-19"}.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    monkeypatch.undo()
    db.migrate(conn, tmp_path / "b")
    store = Store(conn)
    cfg = notify.settings(store)
    on = {t["key"]: t["enabled"] for t in cfg["types"]}
    assert not on["pepite_free"] and not on["pepite_coming"] and on["morning"] and not on["weekly"] and on["health"]
    assert (cfg["pepite_min"], cfg["daily_hour"], cfg["morning_min"], cfg["weekly_day"]) == (80, 7, 50, 4)
    # Le résumé du matin déjà parti aujourd'hui : le quotidien ne repart pas le jour de la mise à jour.
    assert not notify.daily_due(store, datetime(2026, 10, 19, 9, 0))
    assert not store.get("alert_take_min") and not store.get("morning_hour")


# --- Bout en bout : appli, Telegram simulé ---------------------------------------------------------

def _client(logged):
    client = logged()
    st = client.app.state
    sent = []

    async def send(text, chat_id=None, retry=True):
        sent.append(text)

    st.telegram.send = send
    st.store.set("telegram_token", "123:abc")
    st.store.set("telegram_chat_id", "1")
    st.store.set("public_url", "https://nas.exemple")
    st.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('sel', '2010-01-01')")  # le .fr a des données
    st.explorer.invalidate("fr")
    return client, st, sent


def test_alert_settings_card_and_trial(logged):
    client, st, sent = _client(logged)
    cfg = client.get("/api/settings/alerts").json()
    assert [t["key"] for t in cfg["types"]] == list(notify.KEYS) and all(t["enabled"] for t in cfg["types"])
    assert {t["key"]: t["rhythm"] for t in cfg["types"]}["pepite_free"] == "immediate"
    assert {t["key"]: t["rhythm"] for t in cfg["types"]}["views"] == "daily"
    assert (cfg["pepite_min"], cfg["daily_hour"]) == (70, 8) and client.get("/api/settings").json()["alerts"] == cfg
    for bad in ({"pepite_min": 101}, {"daily_hour": 24}, {"weekly_day": 7}, {"enabled": {"inconnu": True}}):
        assert client.put("/api/settings/alerts", json=bad).status_code == 422, bad
    cfg = client.put("/api/settings/alerts", json={"enabled": {"views": False}, "pepite_min": 80, "daily_hour": 7}).json()
    assert not {t["key"]: t["enabled"] for t in cfg["types"]}["views"] and cfg["pepite_min"] == 80 and cfg["daily_hour"] == 7
    for route in ("/api/settings/morning", "/api/settings/digest"):
        assert client.put(route, json={}).status_code in (404, 405)
    # Essai : types et rythmes, la meilleure pépite du moment ; rien n'est noté.
    st.conn.execute("INSERT INTO gandi_checks (name, status, kind, price, checked_at) VALUES ('chat.fr', 'available', 'available', 9, ?)",
                    (iso(datetime.now(timezone.utc)),))
    _opinion(st.conn, "chat.fr", resale=90)
    st.explorer.touch("chat.fr")
    assert client.post("/api/alerts/test").json() == {"sent": True}
    assert sent[-1].startswith("Essai des alertes domain-watch") and "Pépite : chat.fr" in sent[-1]
    assert "Chaque jour à 7 h : " in sent[-1] and "vues enregistrées" not in sent[-1]
    assert st.conn.execute("SELECT COUNT(*) FROM pepites WHERE alerted_at IS NOT NULL").fetchone()[0] == 0
    assert st.store.get("alert_daily_last") == ""
    client.__exit__(None, None, None)


def test_end_to_end_immediate_then_daily_without_duplicates(logged):
    client, st, sent = _client(logged)
    now = datetime.now(timezone.utc)
    client.portal.call(st.send_alerts)  # référence posée, rien à dire
    assert sent == []
    # Une pépite de la Veille, une de l'Explorer, un bon nom ordinaire, une pépite bientôt libre.
    st.drops.record("fr", ["velo", "mai", "lune"], [], now)
    st.conn.execute("UPDATE drops SET state = 'available', available_since = ?, checked_at = ? WHERE name IN ('velo.fr', 'mai.fr')",
                    (iso(now), iso(now)))
    st.conn.execute("UPDATE drops SET state = 'redemption', state_since = ?, release_at = ? WHERE name = 'lune.fr'",
                    (iso(now), iso(now + timedelta(days=5))))
    st.conn.execute("INSERT INTO gandi_checks (name, status, kind, price, checked_at) VALUES ('chat.fr', 'available', 'available', 9, ?)",
                    (iso(now),))
    for name, resale in (("velo.fr", 90), ("chat.fr", 85), ("lune.fr", 80), ("mai.fr", 50)):
        _opinion(st.conn, name, resale=resale, verdict="À prendre" if resale >= 70 else "À surveiller")
        st.explorer.touch(name)
    views = [{"name": "Tout", "filters": {"level": ""}, "alert": True}]
    client.put("/api/explore/views", json={"views": views})
    client.portal.call(st.send_alerts)
    client.portal.call(st.send_alerts)  # rattrapage d'une exécution manquée : rien en double
    assert len(sent) == 1 and sent[0].startswith("Pépites libres : 2 noms")
    assert "velo.fr" in sent[0] and "chat.fr" in sent[0] and "lune.fr" not in sent[0] and "mai.fr" not in sent[0]
    # Aujourd'hui : rubrique et compteur, la même liste.
    today = client.get("/api/today").json()
    assert today["counts"]["pepites"] == 2 and [i["name"] for i in today["pepites"]["free"]] == ["velo.fr", "chat.fr"]
    week = client.get("/api/today", params={"period": "7d"}).json()
    assert week["counts"]["pepites"] == 3 and [i["name"] for i in week["pepites"]["coming"]] == ["lune.fr"]
    # Quotidien : pépite bientôt libre, bons noms et vue sans les pépites déjà annoncées.
    st.store.set("alert_daily_hour", "0")
    st.store.set("alert_on_health", "0")
    client.portal.call(st.send_daily)
    daily = "\n\n".join(sent[1:])
    assert len(sent) == 2 and daily.startswith("Résumé du ")
    assert "Pépites bientôt libres : 1" in daily and "lune.fr" in daily
    assert "mai.fr" in daily and daily.count("velo.fr") == 0 and daily.count("chat.fr") == 0
    assert "Vue « Tout » : 1 nouveau nom" in daily and "Résumé de la semaine" not in daily  # aucun domaine suivi
    # Redémarrage ou nouvel essai le même jour : le quotidien ne repart pas.
    client.portal.call(st.send_daily)
    assert len(sent) == 2
    # Le lendemain : rien de neuf, rien n'est envoyé (la même liste ne revient pas).
    st.conn.execute("UPDATE drops SET available_since = ? WHERE name = 'mai.fr'", (iso(now - timedelta(days=1, hours=1)),))
    st.store.set("alert_daily_last", "2000-01-01")
    client.portal.call(st.send_daily)
    assert len(sent) == 2 and st.store.get("alert_daily_last") != "2000-01-01"
    assert json.loads(st.store.get("view_alert_last"))["Tout"]
    client.__exit__(None, None, None)


def test_domain_change_and_ai_ready_follow_their_boxes(logged):
    client, st, sent = _client(logged)
    client.put("/api/settings/alerts", json={"enabled": {"domain_change": False}})
    client.portal.call(st.checker.notify, "Domaine suivi : velo.fr")
    assert sent == []
    client.put("/api/settings/alerts", json={"enabled": {"domain_change": True}})
    client.portal.call(st.checker.notify, "Domaine suivi : velo.fr")
    assert sent == ["Domaine suivi : velo.fr"]
    # Passe DNS arrêtée : plus de message à part, dite dans la rubrique Santé du quotidien.
    client.portal.call(st.fr_dns._alert, "DNS .fr : passe arrêtée (refus du serveur).")
    assert len(sent) == 1 and "passe arrêtée" in json.loads(st.store.get("health_events"))[-1]["text"]
    client.__exit__(None, None, None)


def test_daily_refused_is_retried_and_unchecked_health_does_not_pile_up(logged):
    client, st, sent = _client(logged)
    refused = []

    async def refuse(text, chat_id=None, retry=True):
        refused.append(text)
        return "Telegram a refusé le message : chat introuvable."

    st.store.set("alert_daily_hour", "0")
    morning.note(st.store, "Gandi : passe de nuit arrêtée (jeton refusé).", datetime.now(timezone.utc))
    real, st.telegram.send = st.telegram.send, refuse
    client.portal.call(st.send_daily)
    # Rien n'est parti : rien n'est noté, le passage suivant réessaie.
    assert len(refused) == 1 and "Santé :" in refused[0] and not st.store.get("alert_daily_last")
    st.telegram.send = real
    client.portal.call(st.send_daily)
    assert len(sent) == 1 and "jeton refusé" in sent[0] and st.store.get("alert_daily_last")
    # Santé décochée : les événements sont effacés au passage, sans message ; rien ne s'accumule.
    client.put("/api/settings/alerts", json={"enabled": {"health": False}})
    morning.note(st.store, "DNS .fr : passe arrêtée (refus du serveur).", datetime.now(timezone.utc))
    st.store.set("alert_daily_last", "2000-01-01")
    client.portal.call(st.send_daily)
    assert len(sent) == 1 and json.loads(st.store.get("health_events")) == []
    client.__exit__(None, None, None)
