import json
import subprocess
import sys
from datetime import timedelta

from app import ai, ai_format, db, escalation
from app.checker import iso, utcnow
from tests.test_app import client_factory, logged  # noqa: F401  mêmes fixtures que l'appli
from tests.test_explore import env  # noqa: F401

TAKE = {"oral": 9, "radio": 9, "memo": 7, "sens": 9, "marque": 6, "marche": 8, "ext": 10, "acheteurs": 7, "risque": 0}
WATCH = {"oral": 8, "radio": 8, "memo": 7, "sens": 8, "marque": 5, "marche": 5, "ext": 9, "acheteurs": 5, "risque": 0}
SKIP = {"oral": 5, "radio": 4, "memo": 4, "sens": 2, "marque": 2, "marche": 2, "ext": 5, "acheteurs": 1, "risque": 0}
NO_CONTEXT = lambda name: {}  # noqa: E731  verdict sur la note de revente (pas de demande mesurée)


def avis(name="lesmotos.com", grid=TAKE, comment="Marché moto à panier moyen. Acheteur type : concession.", **over):
    item = {"name": name, **grid, **over, "comment": comment}
    item.update(ai_format.default_prices(item))
    return item


def test_grids_used_here_give_the_three_verdicts():
    assert [ai_format.derive(avis(grid=g))["verdict"] for g in (TAKE, WATCH, SKIP)] == ["À prendre", "À surveiller", "À laisser"]


def test_take_goes_to_the_panel_and_a_promising_watch_to_the_rescue_once(env):
    assert escalation.consider(env.conn, "a.fr", "À prendre", 40) == "panel"
    assert escalation.consider(env.conn, "b.fr", "À surveiller", 5) == "rescue"
    assert escalation.consider(env.conn, "c.fr", "À surveiller", 4.9) is None
    assert escalation.consider(env.conn, "d.fr", "À laisser", 50) is None
    # Jamais deux fois : un nom déjà dans l'escalade (ou passé) n'y rentre plus ; une demande depuis la fiche le remet.
    assert escalation.consider(env.conn, "a.fr", "À prendre", 40) is None
    assert escalation.counts(env.conn) == {"rescue": 1, "panel": 1}
    escalation.reset(env.conn, "a.fr")
    assert escalation.consider(env.conn, "a.fr", "À prendre", 40) == "panel"


def test_save_sends_new_takes_to_the_panel_and_keeps_the_opinion(env):
    """Plus de contre-notation au plus bas des deux : un « À prendre » garde son avis et part au panel."""
    out = ai.save(env.conn, [avis()], lambda n: True)
    assert out["escalated"] == ["lesmotos.com"]
    assert escalation.state(env.conn, "lesmotos.com")["stage"] == "panel"
    harsh = avis(grid={**TAKE, "acheteurs": 3, "marche": 5})
    assert ai.save(env.conn, [harsh], lambda n: True)["escalated"] == []  # second avis : pas de seconde entrée
    assert "lesmotos.com" not in [n["name"] for n in ai.pending(env, 50)]  # ni dans le lot de nuit


def test_names_are_reserved_45_minutes_per_pass(env):
    for name in ("a.fr", "b.fr"):
        escalation.consider(env.conn, name, "À prendre", 40)
    escalation.consider(env.conn, "c.fr", "À surveiller", 10)
    first = escalation.claim(env.conn, lambda names: [{"name": n} for n in names])
    assert first["claim"] and [n["name"] for n in first["panel"]] == ["a.fr", "b.fr"] and first["rescue"] == [{"name": "c.fr"}]
    assert first["votes"] == {"rescue": 2, "panel": 10}
    # Passage suivant pendant que le premier note encore : rien à reprendre.
    again = escalation.claim(env.conn, lambda names: [{"name": n} for n in names])
    assert again["panel"] == again["rescue"] == [] and again["claim"] == ""
    # Passage interrompu : la réservation échue, les noms reviennent.
    env.conn.execute("UPDATE ai_escalation SET claimed_until = ?", (iso(utcnow() - timedelta(minutes=1)),))
    assert escalation.stale_claims(env.conn) == 3
    assert len(escalation.claim(env.conn, lambda names: names)["panel"]) == 2


def test_rescue_sends_to_the_panel_if_one_counter_opinion_says_take(env):
    for name in ("lesmotos.com", "lespianos.com"):
        escalation.consider(env.conn, name, "À surveiller", 10)
    out = escalation.record(env.conn, "rescue", "c1", 1, [avis("lesmotos.com", WATCH), avis("lespianos.com", WATCH)], NO_CONTEXT)
    assert out["saved"] == 2 and out["final"] == []
    out = escalation.record(env.conn, "rescue", "c1", 2, [avis("lesmotos.com", TAKE), avis("lespianos.com", WATCH)], NO_CONTEXT)
    assert out["final"] == [{"name": "lespianos.com", "notify": False}]
    assert escalation.state(env.conn, "lesmotos.com")["stage"] == "panel" and escalation.state(env.conn, "lespianos.com") is None
    # Un nom pas à cette étape est refusé, sans bloquer les autres.
    out = escalation.record(env.conn, "rescue", "c1", 1, [avis("lesmotos.com", TAKE)], NO_CONTEXT)
    assert out["rejected"] == [{"name": "lesmotos.com", "reason": "pas à cette étape"}]


def panel(env, name, grids, comments=None):
    ai.save(env.conn, [avis(name, TAKE)], lambda n: True)
    for voter, grid in enumerate(grids, 1):
        comment = (comments or {}).get(voter, "Marché moto à panier moyen. Acheteur type : concession.")
        out = escalation.record(env.conn, "panel", "c1", voter, [avis(name, grid, comment)], NO_CONTEXT)
    return out, ai.load(env.conn, [name])[name]


def test_panel_keeps_a_take_backed_by_seven_of_ten_with_the_median(env):
    out, opinion = panel(env, "lesmotos.com", [TAKE] * 8 + [WATCH, SKIP])
    assert out["final"] == [{"name": "lesmotos.com", "notify": False}]
    assert opinion["comment"].startswith("À prendre.") and opinion["grid"] == {**TAKE, "memo": 4}  # memo : calculé
    assert opinion["panel"]["take"] == 8 and opinion["panel"]["n"] == 10 and opinion["panel"]["veto"] == ""
    assert escalation.state(env.conn, "lesmotos.com") is None
    # La décision du panel ne bouge plus au recalcul des notes.
    env.fr.store.set("ai_derive", "")
    ai.rederive(env.conn, env.fr.store)
    assert ai.load(env.conn, ["lesmotos.com"])["lesmotos.com"]["panel"]["take"] == 8


def test_panel_needs_a_qualified_majority(env):
    _, opinion = panel(env, "lesmotos.com", [TAKE] * 6 + [WATCH] * 4)
    # Médiane « À prendre » mais 6 sur 10 seulement : avis partagés, le nom reste à surveiller.
    assert opinion["comment"].startswith("À surveiller.") and opinion["panel"]["median_verdict"] == "À prendre"


def test_three_raters_seeing_a_spelling_fault_veto_the_take(env):
    fault = "Faute : bureaudetudes. Sans le d, la formule est bancale. Acheteur type : bureau d'études."
    _, opinion = panel(env, "bureauetudes.com", [TAKE] * 10, {1: fault, 2: fault, 3: fault})
    assert opinion["panel"]["veto"] == "faute" and opinion["comment"].startswith("À surveiller.")
    _, opinion = panel(env, "lesmotos.com", [{**TAKE, "risque": 5}] * 3 + [TAKE] * 7)
    assert opinion["panel"]["veto"] == "marque" and not opinion["comment"].startswith("À prendre")


def test_panel_median_can_leave_the_name(env):
    _, opinion = panel(env, "lesmotos.com", [SKIP] * 6 + [TAKE] * 4)
    assert opinion["comment"].startswith("À laisser.")


def test_a_rater_sent_twice_counts_once(env):
    ai.save(env.conn, [avis()], lambda n: True)
    for _ in range(3):
        escalation.record(env.conn, "panel", "c1", 1, [avis()], NO_CONTEXT)
    assert env.conn.execute("SELECT COUNT(*) FROM ai_votes").fetchone()[0] == 1


def test_migration_sends_former_takes_to_the_panel(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "b")
    rows = (("a.fr", "À prendre. X. Acheteur type : y.", None, 30), ("b.fr", "À laisser. X. Acheteur type : y.", "x", -3),
            ("c.fr", "À surveiller. X. Acheteur type : y.", None, 6), ("d.fr", "À surveiller. X. Acheteur type : y.", None, 2))
    for name, comment, confirmed, gain in rows:
        conn.execute("INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, "
                     "rated_at, version, confirmed_at, yield) VALUES (?, 50, 50, 0, 0, 0, 100, ?, 'x', 7, ?, ?)",
                     (name, comment, confirmed, gain))
    conn.execute("DELETE FROM ai_escalation")
    sql = (db.MIGRATIONS_DIR / "044_ai_escalation.sql").read_text()
    for statement in sql.split(";"):
        if "INSERT OR IGNORE INTO ai_escalation" in statement:
            conn.execute("\n".join(x for x in statement.splitlines() if not x.lstrip().startswith("--")))
    found = dict(conn.execute("SELECT name, stage FROM ai_escalation").fetchall())
    assert found == {"a.fr": "panel", "b.fr": "panel", "c.fr": "rescue"}  # b.fr : déjà contre-noté, donc « À prendre » un jour


def test_requested_analysis_reports_the_final_verdict_after_the_panel(logged, tmp_path):
    client = logged()
    token = client.post("/api/settings/ai-token").json()["token"]
    sent = []

    async def send(text, chat_id=None):
        sent.append(text)

    client.app.state.telegram.send = send
    client.post("/api/ai/request", json={"name": "velo.fr"})
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/ai/results", headers=headers, json={"results": [avis("velo.fr")]}).json()["saved"] == 1
    assert sent == []  # « À prendre » : le message attend le panel
    assert client.get("/api/explore/fiche", params={"name": "velo.fr"}).json()["ai_escalation"]["stage"] == "panel"

    client.cookies.clear()
    assert client.get("/api/ai/escalation").status_code == 401
    lot = client.get("/api/ai/escalation", headers=headers).json()
    assert [n["name"] for n in lot["panel"]] == ["velo.fr"] and "ai" not in lot["panel"][0] and lot["guide"]
    for voter in range(1, 11):
        answer = client.post("/api/ai/escalation/results", headers=headers, json={
            "stage": "panel", "claim": lot["claim"], "voter": voter, "results": [avis("velo.fr")]}).json()
    assert answer["decided"] == [{"name": "velo.fr", "verdict": "À prendre"}]
    assert len(sent) == 1 and "velo.fr" in sent[0] and "Panel : 10 sur 10" in sent[0]
    assert client.post("/api/ai/escalation/results", headers=headers, json={
        "stage": "panel", "claim": "x", "voter": 11, "results": []}).status_code == 422

    # Le script de la tâche : un fichier par correcteur, rien n'est envoyé si l'un est incomplet.
    lot_path = tmp_path / "panel.json"
    lot_path.write_text(json.dumps({"stage": "panel", "claim": "c", "votes": 2, "names": [{"name": "velo.fr"}]}))
    good, empty = tmp_path / "a.json", tmp_path / "b.json"
    good.write_text(json.dumps([avis("velo.fr")]))
    empty.write_text("[]")
    run = subprocess.run([sys.executable, "scripts/ai_exchange.py", "escalation-push", str(lot_path), str(good), str(empty)],
                         capture_output=True, text=True)
    assert run.returncode != 0 and "velo.fr : pas d'avis" in run.stdout
    client.__exit__(None, None, None)
