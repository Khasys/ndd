"""Listes fournies (termes premium, mots-clés, métier + ville, marques inventées) et « Mes ajouts »."""

import asyncio
import json
from datetime import timezone

import pytest

from app import ai, ccdns, drops, explore, frdns, gandi, quality, value, wordlists
from app.checker import utcnow
from app.rdap import LookupResult
from tests.test_app import PASSWORD, client_factory, logged  # noqa: F401  (fixtures)
from tests.test_explore import FakeRdap, add_fr, env, zone  # noqa: F401  (fixture env)


@pytest.fixture
def mine_dir(tmp_path):
    """Mes ajouts dans un dossier de test ; vidés à la fin (l'univers partagé ne garde rien d'un test à l'autre)."""
    wordlists.configure(tmp_path / "mine")
    yield tmp_path / "mine"
    wordlists.configure(None)


# --- Listes fournies -----------------------------------------------------------------------------------


def test_provided_lists_are_loaded_with_labels_and_languages():
    lists = wordlists.lists()
    for key, label in (("premium", "Termes premium"), ("keyword", "Mots-clés à forte valeur"),
                       ("local", "Métier + ville"), ("brand", "Marques inventées"), ("mine", "Mes ajouts")):
        assert wordlists.LABELS[key] == label and key in explore.KINDS and key in explore.MAP_ROWS
    assert {"cryptomonnaie", "refinancing"} <= lists["premium"] and "rachatcredit" in lists["keyword"]
    assert "courtiercreditparis" in lists["local"] and "kinia" in lists["brand"]
    assert wordlists.categories("kinia") == ["brand"]
    # Langue relevée par scripts/build_lists.py --provided : celle des mots qui composent le nom.
    assert wordlists.langs_of("mutuellesante") == ["fr"] and wordlists.langs_of("carinsurance") == ["en"]
    assert wordlists.langs_of("kinia") == []  # marque inventée : sans langue, comme un nom propre
    assert "brand" in wordlists.NO_LANG and "mine" in wordlists.NO_LANG


def test_provided_list_entries_out_of_format_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(wordlists, "DATA", tmp_path)
    (tmp_path / "premium.txt").write_text("credit\nCrédit\nbon plan\nluxe\ncredit\n-ab\n\n", encoding="utf-8")
    assert wordlists._ordered("premium.txt") == ["credit", "luxe"]
    assert wordlists._ordered("local.txt") == []  # pas encore livrée : vide, pas d'erreur


def test_build_lists_finds_the_language_of_compound_names():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import build_lists

    vocab = {"fr": {"plombier", "paris", "insu", "rance", "car", "credit", "auto"},
             "en": {"car", "insurance", "credit", "auto"}}
    known = {"fr": {"maison"}, "en": {"house"}}
    found = build_lists.provided_langs(["plombierparis", "carinsurance", "creditauto", "zqxv", "maison"], known, vocab)
    # La découpe la plus économe l'emporte (car + insurance, pas car + insu + rance) ; un nom connu est laissé.
    assert found == {"plombierparis": ["fr"], "carinsurance": ["en"], "creditauto": ["fr", "en"]}


def test_provided_lists_count_in_the_score():
    # Rang pris en compte comme pour les autres listes, avec le poids de la liste calée la plus proche.
    f = quality.features("rachatcredit", "fr")
    assert f["in_sector"] > 0.9 and f["meaning"] > 0.9
    assert "12e mot-clé à forte valeur" in value.score("leasingvoiture", [])["reasons"]
    assert "2e terme premium" in value.score("rachatcredit", [])["reasons"]  # aussi mot-clé : la liste la mieux placée
    assert quality.features("courtiercreditparis", "fr")["in_combo"] > 0
    # Marque inventée : la sonorité et le rang comptent, pas le sens ; jamais « faible » d'office.
    brand = quality.features("kinia", "com")
    assert brand["meaning"] == 0 and brand["in_startup"] > 0.9 and brand["lang_mismatch"] == 0
    assert value.quality("vexio") != "word"
    for tld in ("fr", "com"):
        found = value.score("kinia", [], tld=tld)
        assert found["score"] >= explore.LEVEL_GOOD and "1er nom de marque inventé" in found["reasons"]
        # Mieux placé dans sa liste, mieux noté qu'un même mot inventé hors liste.
        assert found["score"] > value.score("zovanu", [], tld=tld)["score"]


def test_provided_lists_wait_for_the_next_zone_read(env):
    # Lecture faite avant l'arrivée des listes fournies : leurs noms sont masqués, pas « libres ».
    zone(env, [], ["maison"])
    env.drops._write(env.drops._path("com", "searched"), "\n".join(sorted(
        set().union(*(n for k, n in wordlists.lists().items() if k not in wordlists.LATE)))).encode())
    env.invalidate("com")
    u = env.universe
    status = env.status_bytes("com")
    assert status[u.index("kinia")] == explore.INVALID and status[u.index("mutuellesante")] == explore.INVALID
    assert status[u.index("maison")] == explore.REGISTERED and status[u.index("courir")] == explore.ABSENT
    # Lecture suivante : cherchés, ils prennent leur état.
    zone(env, [], ["maison", "kinia"])
    env.invalidate("com")
    status = env.status_bytes("com")
    assert status[u.index("kinia")] == explore.REGISTERED and status[u.index("mutuellesante")] == explore.ABSENT


def test_provided_lists_wait_for_the_next_fr_import(env):
    add_fr(env, "maison")
    u = env.universe
    # Import .fr d'avant le relevé des noms cherchés : les listes ajoutées depuis attendent le suivant.
    status = env.status_bytes("fr")
    assert status[u.index("kinia")] == explore.INVALID and status[u.index("courir")] == explore.ABSENT
    # La passe DNS du .fr les interroge quand même ; délégué, le nom est pris.
    checker = frdns.FrDns(env.conn, env.fr.store, env, timezone.utc)
    assert "kinia" in checker.pending(1_000_000)
    env.conn.execute("INSERT INTO name_dns (label, present, checked_at) VALUES ('kinia', 1, '2026-10-18T01:00:00Z')")
    env.invalidate("fr")
    assert env.status_bytes("fr")[u.index("kinia")] == explore.REGISTERED
    # Import fait avec ces listes (fr.searched) : plus rien de masqué.
    env.fr._save_searched(wordlists.listed())
    env.conn.execute("DELETE FROM name_dns")
    env.invalidate("fr")
    assert env.status_bytes("fr")[u.index("kinia")] == explore.ABSENT


def test_provided_lists_wait_for_the_next_dns_pass(env):
    """.co, .ai, .io : un nom jamais interrogé reste « Pas interrogé » (hors des listes et des compteurs) ; les
    listes fournies entrent dans la passe (.co : toutes ; .ai et .io : marques inventées aussi)."""
    at = "2026-10-18T03:00:00+00:00"
    env.conn.execute("INSERT INTO cc_dns (name, present, checked_at, seen_at) VALUES ('maison.co', 1, ?, ?)", (at, at))
    env.fr.store.set("co_dns_status", json.dumps({"finished_at": at, "checked": 1}))
    env.invalidate("co")
    u = env.universe
    status = env.status_bytes("co")
    assert status[u.index("maison")] == explore.REGISTERED and status[u.index("kinia")] == explore.INVALID
    assert env.card("kinia")["items"][-1]["status_label"] == explore.NOT_ASKED
    for tld, names in (("co", {"kinia", "rachatcredit", "courtiercreditparis"}), ("io", {"kinia"})):
        assert names <= set(ccdns.CcDns(tld, env.conn, env.fr.store, env, timezone.utc).pending(ccdns.PER_TLD))
    # Interrogé : absent du DNS, « à vérifier » comme les autres.
    env.conn.execute("INSERT INTO cc_dns (name, present, checked_at) VALUES ('kinia.co', 0, ?)", (at,))
    env.fr.store.set("co_dns_status", json.dumps({"finished_at": at, "checked": 2}))
    assert env.status_bytes("co")[u.index("kinia")] == explore.ABSENT


# --- Mes ajouts ------------------------------------------------------------------------------------


def test_labels_are_normalised_or_refused():
    assert wordlists.normalize("  Mon-Café.fr ") == "mon-cafe" and wordlists.normalize("ŒUVRE") == "oeuvre"
    for bad in ("", ".fr", "mon projet", "-abc", "abc-", "a_b", "x" * 64, "projet!"):
        with pytest.raises(wordlists.InvalidLabel):
            wordlists.normalize(bad)
    assert wordlists.normalize("x" * 63) == "x" * 63


def test_mine_is_persistent_and_ordered(mine_dir):
    assert wordlists.mine() == ()
    assert wordlists.add_mine("monsuperprojet") and not wordlists.add_mine("monsuperprojet")
    assert wordlists.add_mine("autreprojet")
    assert (mine_dir / wordlists.MINE_FILE).read_text() == "monsuperprojet\nautreprojet\n"
    wordlists.configure(mine_dir)  # redémarrage : relu dans le dossier de données
    assert wordlists.mine() == ("monsuperprojet", "autreprojet")
    assert wordlists.ranks()["mine"] == {"monsuperprojet": 1, "autreprojet": 2}
    assert b"monsuperprojet" in wordlists.watched() and "monsuperprojet" in wordlists.listed()
    assert wordlists.remove_mine("autreprojet") and not wordlists.remove_mine("autreprojet")
    assert wordlists.mine() == ("monsuperprojet",)


def test_mine_enters_the_universe_without_restart(env, mine_dir):
    zone(env, [], ["maison"])  # lecture de .com faite avant l'ajout
    add_fr(env, "maison")
    before = env.universe
    assert before.index("monsuperprojet") is None and not env.card("monsuperprojet")["known"]
    wordlists.add_mine("monsuperprojet")
    wordlists.add_mine("zz9q")  # nom court : déjà dans l'univers, il prend un sens
    seconds = env.reload_lists()
    u = env.universe
    assert u is not before and u.size == before.size + 1 and seconds < 30
    i = u.index("monsuperprojet")
    assert u.label(i) == "monsuperprojet" and u.words == sorted(u.words)
    assert u.good[i] and u.good[u.index("zz9q")] and u.base_table[i] & 127 > 0
    assert i in u.by_list["mine"] and u.index("zz9q") in u.by_list["mine"]
    assert u.label(u.index("navarro")) == "navarro"  # les autres noms restent à leur place dans l'ordre
    card = env.card("monsuperprojet")
    assert card["known"] and card["mine"] and card["categories"] == ["mine"]
    items = {item["tld"]: item for item in card["items"]}
    # Un score, sans point pour le rang dans Mes ajouts (l'ordre d'ajout ne classe rien).
    assert items["com"]["score"] > 0 and value.popularity("monsuperprojet") == (0, None)
    assert value.score("monsuperprojet", [], tld="com")["score"] == items["com"]["score"]
    # Pas encore cherché dans la zone ni dans le fichier .fr : masqué jusqu'à la prochaine lecture.
    assert items["com"]["status"] == explore.INVALID and items["fr"]["status"] == explore.INVALID
    assert env.fiche("monsuperprojet.com")["mine"] is True
    # Lecture suivante de la zone : cherché, il prend son état.
    zone(env, [], ["maison"])
    env.invalidate("com")
    assert env.status_bytes("com")[i] == explore.ABSENT
    assert env.search("com", q="monsuperprojet", mode="exact", category="mine")["items"][0]["label"] == "monsuperprojet"
    assert env.facets("com", category="mine", junk=True)["maybe_free"] == 2
    # Passe DNS du .fr : interrogé d'ici le prochain fichier.
    assert "monsuperprojet" in frdns.FrDns(env.conn, env.fr.store, env, timezone.utc).pending(1_000_000)
    # .co, .ai, .io : interrogés à la prochaine passe, en tête, quel que soit leur score.
    for tld in ccdns.TLDS:
        assert ccdns.CcDns(tld, env.conn, env.fr.store, env, timezone.utc).pending(5)[0] == "monsuperprojet"
    # Retiré : hors de l'univers.
    wordlists.remove_mine("monsuperprojet")
    env.reload_lists()
    assert env.universe.index("monsuperprojet") is None and not env.card("monsuperprojet")["known"]


def test_mine_checked_at_once_then_in_gandi_and_ai_queues(env, mine_dir):
    zone(env, [], ["maison"])
    wordlists.add_mine("monsuperprojet")
    env.reload_lists()
    # Vérification au moment de l'ajout (RDAP du nom demandé) : l'état vaut d'ici la prochaine lecture de zone.
    env.drops.rdap = FakeRdap({"monsuperprojet.com": LookupResult(available=True)})
    item = asyncio.run(env.verify("monsuperprojet.com"))
    assert item["status"] == explore.FREE and "mine" in item["categories"]
    names = [n["name"] for n in ai.pending(env, 50)]
    assert "monsuperprojet.com" in names
    night = gandi.NightPass(gandi.Gandi(env.fr.store, conn=env.conn), env.conn, env.fr.store, env, timezone.utc)
    assert night.pending(50)[0] == "monsuperprojet.com"


def test_mine_routes_session_only(logged, tmp_path):
    client = logged()
    state = client.app.state
    try:
        async def lookup(name):
            return LookupResult(available=True)

        state.drops.rdap.lookup = lookup
        assert client.get("/api/explore/name", params={"label": "monsuperprojet"}).json()["known"] is False
        assert client.post("/api/explore/mine", json={"name": "mon projet"}).status_code == 422
        assert client.post("/api/explore/mine", json={"name": "-x"}).status_code == 422
        found = client.post("/api/explore/mine", json={"name": "MonSuperProjet.com"}).json()
        assert found["label"] == "monsuperprojet" and found["added"] and found["count"] == 1
        assert found["check"]["name"] == "monsuperprojet.com" and found["check"]["status"] == explore.FREE
        assert (tmp_path / wordlists.MINE_FILE).read_text() == "monsuperprojet\n"
        card = client.get("/api/explore/name", params={"label": "monsuperprojet"}).json()
        assert card["known"] and card["mine"] and card["categories"] == ["mine"]
        fiche = client.get("/api/explore/fiche", params={"name": "monsuperprojet.com"}).json()
        assert fiche["mine"] and fiche["known"]
        # Sans extension : ajouté, rien à vérifier.
        again = client.post("/api/explore/mine", json={"name": "monsuperprojet"}).json()
        assert again["added"] is False and again["check"] is None
        top = client.get("/api/explore/top", params={"cat": "mine", "n": 10}).json()
        assert [i["label"] for i in top["items"]] == ["monsuperprojet"] and top["lists"]["mine"] == "Mes ajouts"
        # Jeton en lecture seule : ni ajout ni retrait.
        token = client.post("/api/settings/ai-token").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.cookies.clear()
        assert client.post("/api/explore/mine", headers=headers, json={"name": "autre"}).status_code == 401
        assert client.delete("/api/explore/mine/monsuperprojet", headers=headers).status_code == 401
        assert client.post("/login", data={"username": "moi", "password": PASSWORD}).status_code == 303
        assert client.delete("/api/explore/mine/monsuperprojet").json()["removed"] is True
        assert client.delete("/api/explore/mine/monsuperprojet").status_code == 404
        assert client.get("/api/explore/name", params={"label": "monsuperprojet"}).json()["known"] is False
        assert wordlists.mine() == ()
    finally:
        client.__exit__(None, None, None)
        wordlists.configure(None)
