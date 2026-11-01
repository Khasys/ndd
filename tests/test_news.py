"""Radar des nouveautés (news.py) : noms proposés par la tâche IA, vérifiés en .fr et en .com, notés, annoncés."""

import json
import time
from datetime import datetime, timedelta, timezone

from app import news, news_format, wordlists
from app.rdap import DomainInfo, LookupResult

from tests.test_alerts import _client
from tests.test_app import client_factory, logged  # noqa: F401  (fixtures)


def _item(label, lang="fr", kind="mot", reason="Mot repris partout depuis une semaine dans la presse.", source="https://media.exemple/article"):
    return {"label": label, "lang": lang, "kind": kind, "reason": reason, "source": source}


def test_proposed_name_rules():
    assert news_format.clean(_item("Écoanxiété"))[0]["label"] == "ecoanxiete"
    assert news_format.clean(_item("rizz.com"))[0]["label"] == "rizz"  # extension retirée
    for bad, why in ((_item("ab"), "3 à 30"), (_item("rizz", lang="de"), "langue"), (_item("rizz", kind="marque"), "nature"),
                     (_item("rizz", reason="court"), "raison"), (_item("rizz", source=" "), "source"),
                     (_item("2026"), "chiffres"), (_item("mon--mot"), "double tiret"), ("rizz", "objet")):
        found, reason = news_format.clean(bad)
        assert found is None and why in reason, (bad, reason)
    long = news_format.clean(_item("rizz", reason="x " * 400))[0]
    assert len(long["reason"]) == news_format.MAX_TEXT


def test_news_list_has_its_language(tmp_path):
    wordlists.configure(tmp_path)
    try:
        assert wordlists.set_news([("rizz", "en"), ("ecoanxiete", "fr"), ("rizz", "fr")])
        assert not wordlists.set_news([("rizz", "en"), ("ecoanxiete", "fr")])  # inchangée : rien n'est rechargé
        assert wordlists.news() == ("rizz", "ecoanxiete")
        assert "rizz" in wordlists.languages()["en"] and "ecoanxiete" in wordlists.languages()["fr"]
        assert wordlists.lists()[wordlists.NEWS] == {"rizz", "ecoanxiete"}
        assert wordlists.LABELS[wordlists.NEWS] == "Nouveautés"
    finally:
        wordlists.configure(None)


def _wait(client, condition, tries=1200):
    for _ in range(tries):
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_news_radar_end_to_end(logged, monkeypatch):
    """Proposés par la tâche, vérifiés au registre (hors Veille), notés par l'IA à partir de la raison de l'actualité,
    annoncés une seule fois sur Telegram, sans « Analyse IA prête » ni doublon en pépite."""
    monkeypatch.setattr(news, "FR_GAP", 0)
    client, st, sent = _client(logged)
    conn = st.conn
    token = client.post("/api/settings/ai-token").json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    future = datetime.now(timezone.utc) + timedelta(days=300)
    taken = {"rizz.com"}

    async def lookup(name):
        if name in taken:
            return LookupResult(found=True, available=False, info=DomainInfo(statuses=["active"], expires_at=future))
        return LookupResult(available=True)

    st.fr.rdap.lookup = lookup
    st.drops.rdap.lookup = lookup
    client.portal.call(st.send_alerts)  # référence des pépites, rien à dire

    # Jeton IA seulement ; la consigne et rien de déjà proposé.
    assert client.get("/api/ai/news").status_code == 200  # session ouverte
    client.cookies.clear()
    assert client.get("/api/ai/news").status_code == 401
    radar = client.get("/api/ai/news", headers=headers).json()
    assert "Interdits" in radar["guide"] and radar["known"] == [] and radar["recent"] == [] and radar["max"] == 60
    assert "deux tiers des noms" in radar["guide"] and "repérés tôt, pour le .com" in radar["guide"]

    sent_items = [_item("rizz", lang="en", reason="Mot de l'année d'un dictionnaire anglais, repris par la presse."),
                  _item("Écoanxiété"), _item("rizz"), _item("nom", lang="de"), _item("sans", source="")]
    answer = client.post("/api/ai/news", json={"items": sent_items}, headers=headers).json()
    assert answer["saved"] == ["rizz", "ecoanxiete"]
    assert [(r["label"], r["reason"].split(" ")[0]) for r in answer["rejected"]] == [("rizz", "doublon"), ("nom", "langue"),
                                                                                       ("sans", "source")]
    assert client.post("/api/ai/news", json={"items": [_item("x" * 5)] * 61}, headers=headers).status_code == 422

    assert _wait(client, lambda: conn.execute("SELECT COUNT(*) FROM news WHERE checked_at IS NOT NULL").fetchone()[0] == 2)
    states = {r["label"]: (r["fr"], r["com"]) for r in conn.execute("SELECT * FROM news")}
    assert states == {"rizz": ("free", "taken"), "ecoanxiete": ("free", "free")}
    # Libres : avis demandés, hors de la cloche ; pris : rien à noter.
    asked = {r["name"]: r["source"] for r in conn.execute("SELECT name, source FROM ai_requests")}
    assert asked == {"rizz.fr": "news", "ecoanxiete.fr": "news", "ecoanxiete.com": "news"}
    lot = client.get("/api/ai/pending", params={"requested": "1"}, headers=headers).json()
    assert {n["name"] for n in lot["names"]} == set(asked)
    rizz = next(n for n in lot["names"] if n["name"] == "rizz.fr")
    assert "Mot de l'année" in rizz["actualite"] and "anglais" in rizz["actualite"] and "news" in rizz["categories"]
    assert wordlists.news() == ("ecoanxiete", "rizz") or set(wordlists.news()) == {"ecoanxiete", "rizz"}

    # Connexion de l'utilisateur : rien dans la cloche ni dans la Veille ; la raison sur la fiche.
    client.post("/login", data={"username": "moi", "password": "motdepasse-de-test"})
    assert client.get("/api/ai/requests").json() == {"pending": [], "done": []}
    assert all(i["name"] not in asked for i in client.get("/api/drops").json()["items"])
    fiche = client.get("/api/explore/fiche", params={"name": "rizz.fr"}).json()
    assert fiche["item"]["reasons"][0].startswith("Nouveauté du ") and "Mot de l'année" in fiche["item"]["reasons"][0]

    def opinion(name, verdict, resale):
        return {"name": name, "exploit": min(50, resale), "resale": resale, "quick_low": 100, "quick_high": 300,
                "final_low": 1000, "final_high": 3000, "comment": f"{verdict}. Mot nouveau très repris. Acheteur type : média."}

    results = [opinion("rizz.fr", "À prendre", 80), opinion("ecoanxiete.fr", "À surveiller", 50),
               opinion("ecoanxiete.com", "À laisser", 20)]
    saved = client.post("/api/ai/results", json={"results": results}, headers=headers).json()
    assert saved["saved"] == 3 and sent == [], saved  # pas de message « Analyse IA prête »

    client.portal.call(st.send_alerts)
    client.portal.call(st.send_alerts)  # rien en double, ni comme pépite
    assert len(sent) == 1, sent
    assert sent[0].startswith("Nouveauté libre : rizz.fr") and "Mot de l'année" in sent[0] and "revente 80" in sent[0]
    assert "À surveiller : ecoanxiete.fr" in sent[0] and "ecoanxiete.com" not in sent[0]
    assert "https://nas.exemple/?fiche=rizz.fr" in sent[0]

    # Passage suivant : déjà proposés, et ce que l'IA en a pensé.
    radar = client.get("/api/ai/news", headers=headers).json()
    assert set(radar["known"]) == {"rizz", "ecoanxiete"}
    recent = {r["label"]: r for r in radar["recent"]}
    assert recent["rizz"]["com"] == "taken" and recent["rizz"]["verdicts"] == {"fr": "À prendre"}
    again = client.post("/api/ai/news", json={"items": [_item("rizz")]}, headers=headers).json()
    assert again["saved"] == [] and again["rejected"][0]["reason"].startswith("déjà proposé le ")

    diag = client.get("/api/diag").json()["news"]
    assert diag["total"] == 2 and diag["to_check"] == 0
    assert diag["by_tld"]["fr"]["states"] == {"free": 2} and diag["by_tld"]["fr"]["alerted"] == 1
    kinds = dict(conn.execute("SELECT name, kind FROM alert_sent WHERE kind LIKE 'news%'").fetchall())
    assert kinds == {"rizz.fr": "news", "ecoanxiete.fr": "news_seen", "ecoanxiete.com": "news_seen"}
    assert diag["by_tld"]["fr"]["verdicts"] == {"À prendre": 1, "À surveiller": 1}
    assert diag["by_tld"]["com"]["states"] == {"free": 1, "taken": 1}
    client.__exit__(None, None, None)


def test_manual_request_wins_over_news_request(logged):
    """Une demande depuis la fiche en attente n'est pas reprise par le radar ; l'inverse la rend visible."""
    from app import ai

    client = logged()
    conn = client.app.state.conn
    ai.request(conn, "rizz.fr")
    assert ai.request(conn, "rizz.fr", news.SOURCE)["source"] is None
    ai.request(conn, "chat.fr", news.SOURCE)
    assert [r["name"] for r in client.get("/api/ai/requests").json()["pending"]] == ["rizz.fr"]
    ai.request(conn, "chat.fr")
    assert {r["name"] for r in client.get("/api/ai/requests").json()["pending"]} == {"rizz.fr", "chat.fr"}
    assert ai.asked(conn) == {"rizz.fr", "chat.fr"}
    client.__exit__(None, None, None)


def test_news_reason_is_short_on_a_card():
    from app import explore

    text = explore.news_reason({"reason": "mot " * 80, "added_at": "2026-11-01T07:43:00+00:00"})
    assert text.startswith("Nouveauté du 01/11 : ") and text.endswith("…") and len(text) < 170
    assert json.dumps(text)


def test_touch_does_not_wait_for_a_universe_rebuild(tmp_path):
    """Un nom vérifié pendant que l'univers se refait dans un autre fil : touch rend la main tout de suite, et le nom
    est corrigé à la fin de la reconstruction (la boucle de l'appli n'attend plus plusieurs secondes)."""
    import threading

    from app import explore

    class Ex(explore.Explorer):
        def __init__(self):  # le strict nécessaire de l'Explorer pour touch et la reconstruction
            self._swap_lock = threading.Lock()
            self._memo_lock = threading.RLock()
            self._deferred = set()
            self.touched = []
            self._universe, self._lists_version = object(), wordlists.version

        def _swap_to(self, version):
            self._lists_version = version

    ex = Ex()
    seen = []
    real = explore.Explorer.touch

    def touch(self, name):
        real(self, name) if self._swap_lock.locked() else seen.append(name)

    Ex.touch = touch
    with ex._swap_lock:
        started = time.monotonic()
        ex.touch("rizz.fr")
        assert time.monotonic() - started < 0.5 and ex._deferred == {"rizz.fr"} and seen == []
    ex._lists_version = -1
    ex._swap_universe()
    assert seen == ["rizz.fr"] and ex._deferred == set()


def test_news_context_reaches_every_rating(logged):
    """« chokbar » : le correcteur ne connaît pas forcément le mot. La raison de l'actualité est jointe partout où
    l'IA le note (demande, file de nuit, repêchage et panel de dix passent par describe_names ou pending), et la
    consigne dit de ne pas lui donner sens 0 pour autant."""
    from app import ai

    client = logged()
    st = client.app.state
    st.conn.execute("INSERT INTO news (label, lang, kind, reason, source, added_at) VALUES ('chokbar', 'fr', 'expression', "
                    "'Expression d''argot reprise partout pour dire choqué.', 'https://media.exemple/a', '2026-11-01T06:43:00+00:00')")
    items = {i["name"]: i for i in ai.describe_names(st.explorer, ["chokbar.fr", "chokbar.com", "velo.fr"])}
    assert items["chokbar.fr"]["actualite"] == ("Nouveauté vue dans l'actualité le 01/11/2026 (expression, français) : "
                                                "Expression d'argot reprise partout pour dire choqué.")
    assert "actualite" in items["chokbar.com"] and "actualite" not in items["velo.fr"]
    assert [i.get("actualite") is not None for i in ai.with_news(st.conn, [{"name": "chokbar.io"}, {"name": "x.fr"}])] == [True, False]
    assert "un nom qui porte « actualite »" in ai.GUIDE and "pas 0 parce que le mot n'est pas dans tes connaissances" in ai.GUIDE
    # Lot de la tâche du quart d'heure, une ligne par nom (format compact) : la raison y est.
    from scripts.ai_exchange import line
    assert "Expression d'argot" in line(items["chokbar.fr"])
    client.__exit__(None, None, None)
