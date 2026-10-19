"""Listes fournies (termes stars, mots-clés, métier + ville, marques inventées) et « Mes ajouts »."""

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
    for key, label in (("premium", "Termes stars"), ("keyword", "Mots-clés à forte valeur"),
                       ("local", "Métier + ville"), ("brand", "Noms de marque"), ("mine", "Mes ajouts")):
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
    assert "2e terme star" in value.score("rachatcredit", [])["reasons"]  # aussi mot-clé : la liste la mieux placée
    assert quality.features("courtiercreditparis", "fr")["in_combo"] > 0
    # Marque inventée : la sonorité et le rang comptent, pas le sens ; jamais « faible » d'office.
    brand = quality.features("kinia", "com")
    assert brand["meaning"] == 0 and brand["in_startup"] > 0.9 and brand["lang_mismatch"] == 0
    assert value.quality("vexio") != "word"
    for tld in ("fr", "com"):
        found = value.score("kinia", [], tld=tld)
        assert found["score"] >= explore.LEVEL_GOOD and "4e nom de marque" in found["reasons"]
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


# --- Catégories dérivées -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def build():
    """scripts/build_lists.py et le vocabulaire des règles, tiré des vraies listes (une seconde)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import build_lists

    lists = {key: wordlists._ordered(file) for key, file in wordlists.FILES.items()}
    return build_lists, build_lists.derive_vocab(lists)


def test_sectors_and_trends_are_also_premium(build):
    b, vocab = build
    found = b.derive({"sector": ["assurance", "mortgage"], "trend": ["padel", "agents"], "premium": ["agents"],
                      "word": ["assurance"]}, vocab)
    assert found["assurance"] == ["premium"] and found["mortgage"] == ["premium"] and found["padel"] == ["premium"]
    assert "premium" not in found["agents"]  # déjà terme star : rien à gagner (Tech et startups, par son mot)
    for name in ("assurance", "credit", "mortgage", "assuranceauto", "creditimmobilier"):
        assert name in wordlists.classes()["premium"] and name not in wordlists.lists()["premium"]


def test_combinations_with_a_search_intent_are_keywords(build):
    b, vocab = build
    for name in ("assurancepascher", "comparatifbanque", "bestinsurance", "insurancequotes", "creditenligne",
                 "mortgagefinder", "assurancedevis", "moversquotes"):
        assert b.is_keyword(name, vocab), name
    # Possessifs, médias, ajouts de marque, villes, devis d'autre chose qu'un métier ou un service : pas des requêtes.
    for name in ("monassurance", "mytravel", "insurancehub", "theinsurance", "hairexpress", "assurancepro",
                 "sosassurance", "plombierparis", "infocredit", "assurance", "buyteeth", "matelasdevis", "churchquotes"):
        assert not b.is_keyword(name, vocab), name
    found = b.derive({"combo": ["assurancepascher", "monassurance"], "keyword": ["comparateurassurance"]}, vocab)
    assert found == {"assurancepascher": ["keyword"]}


def test_trade_and_city_names_are_local(build):
    b, vocab = build
    for name in ("plombierparis", "plombier-lyon", "arlingtonlawyer", "fortworthdentist", "injurylawyernewyork",
                 "lyonimmobilier", "plombieriledefrance", "dallasplumber"):
        assert b.is_local(name, vocab), name
    # Ville française d'abord (sauf immo, immobilier, auto, garage), ville seule, numéro, pas de métier.
    for name in ("gapinsurance", "parisplombier", "plombier92", "assurancepascher", "paris", "lyonmagazine",
                 "contactlens"):
        assert not b.is_local(name, vocab), name
    found = b.derive({"combo": ["plombierlyon", "arlingtonlawyer"], "keyword": ["climaticienlyon"],
                      "local": ["plombierparis"], "niche": ["forumparis"]}, vocab)
    assert found["plombierlyon"] == ["job", "local"] and found["arlingtonlawyer"] == ["job", "local"]
    assert "local" in found["climaticienlyon"] and "local" not in found.get("plombierparis", [])


def test_compound_names_get_the_theme_of_their_words(build):
    b, vocab = build
    cases = {"coachsportif": {"sport", "job"}, "assuranceanimaux": {"animal"}, "aiagents": {"startup", "trend"},
             "plombierlyon": {"job"}, "marathonparis": {"sport"}, "journaldesrecettes": {"food"},
             "petsitting": {"animal"}, "dogfinder": {"animal"}, "stockagecloud": {"startup", "trend"}}
    for name, themes in cases.items():
        assert b.theme_of(name, vocab) == themes, name
    # Mot entier seulement, mots trop généraux ou ambigus écartés, mot de 3 lettres seulement entre de vrais mots.
    for name, theme in (("marathon", "food"), ("bearmarket", "animal"), ("theinsurance", "food"),
                        ("terrainconstructible", "sport"), ("assuranceauto", "startup"), ("insurancehub", "startup"),
                        ("mychat", "animal"), ("colocationhosting", "animal"), ("telephonie", "food"),
                        ("expertassurance", "job"), ("permismoto", "sport"), ("campdeteaventure", "food")):
        assert theme not in b.theme_of(name, vocab), name
    # Cuisiniste dans une liste commerciale, cuisine dans un site de niche.
    assert "food" not in b.theme_of("cuisine-metz", vocab, trade=True)
    assert "food" in b.theme_of("forumcuisine", vocab)
    assert b.splits("marathon", vocab["pieces"]) == [("marathon",)]


def test_derived_categories_never_reach_dictionaries_brands_or_mine(build, tmp_path, monkeypatch):
    b, vocab = build
    found = b.derive({"combo": ["plombierlyon"], "word": ["plombierlyon"], "brand": ["kinia"], "mine": ["chienlyon"],
                      "english": ["dogsitting"]}, vocab)
    assert set(found) == {"plombierlyon"}
    assert not {k for keys in found.values() for k in keys} & set(wordlists.NEVER_DERIVED)
    # Fichier écrit : une catégorie interdite ou un nom hors des listes y sont ignorés à la lecture.
    written = (wordlists.DATA / wordlists.DERIVED_FILE).read_text(encoding="utf-8")
    assert not {k for line in written.splitlines() for k in line.split("\t")[1].split(",")} & set(wordlists.NEVER_DERIVED)
    (tmp_path / wordlists.DERIVED_FILE).write_text("assurance\tword,brand,mine,premium\nzzqxv\tjob\n", encoding="utf-8")
    monkeypatch.setattr(wordlists, "DATA", tmp_path)
    monkeypatch.setattr(wordlists, "static_lists", lambda: {"word": frozenset({"assurance"})})
    wordlists.derived.cache_clear()
    try:
        assert wordlists.derived() == {"premium": frozenset({"assurance"})}
    finally:
        monkeypatch.undo()
        wordlists.derived.cache_clear()
        wordlists.classes.cache_clear()


def test_categories_are_the_union_of_lists_and_derived():
    assert wordlists.categories("assurance") == ["word", "english", "premium"]  # sector : liste sans puce
    assert wordlists.own("assurance") == ["word", "english", "sector"]
    assert wordlists.categories("plombierlyon") == ["combo", "job", "local"]
    assert set(wordlists.categories("coachsportif")) == {"sport", "job", "premium", "keyword"}
    assert "animal" in wordlists.categories("assuranceanimaux") and "startup" in wordlists.categories("aiagents")
    assert wordlists.categories("monassurance") == ["combo"] and wordlists.categories("kinia") == ["brand"]
    assert wordlists.LABELS["media"] == "Noms de médias" and "sector" not in wordlists.SHOWN
    assert "sector" not in explore.KINDS and "sector" not in explore.MAP_ROWS and "sector" not in drops.KINDS
    assert wordlists.canonical("sector") == "premium" and explore.split_cats("sector,premium,job") == ["job", "premium"]
    # Rien de dérivé n'entre dans les listes d'origine : ce sont elles que voit le score.
    assert "plombierlyon" not in wordlists.lists()["job"] and "plombierlyon" in wordlists.classes()["job"]


def test_derived_categories_do_not_change_the_score(monkeypatch):
    """Les catégories dérivées ne servent qu'à classer : mêmes traits, même score de base (l'octet de scores.bin) et
    même score complet sur 1 500 noms tirés au hasard, avec ou sans catégories dérivées."""
    import random

    lists = wordlists.static_lists()
    pool = sorted(set().union(*(lists[k] for k in ("combo", "keyword", "local", "premium", "sector", "trend", "niche",
                                                     "media", "expr", "word"))))
    sample = random.Random(7).sample(pool, 1500) + ["assurance", "plombierlyon", "coachsportif", "aiagents"]

    def scores():
        return [(quality.features(n, tld), value.base(n), value.score(n, ["com"], tld=tld)["score"])
                for n in sample for tld in ("fr", "com")]

    before = scores()
    everything = frozenset(sample)
    monkeypatch.setattr(wordlists, "derived", lambda: {k: everything for k in wordlists.LABELS
                                                        if k not in wordlists.NEVER_DERIVED})
    wordlists.classes.cache_clear()
    try:
        assert "food" in wordlists.categories("plombierlyon")  # dérivées bien changées
        assert scores() == before
    finally:
        monkeypatch.undo()
        wordlists.classes.cache_clear()


def test_score_fingerprint_ignores_derived_categories(tmp_path, monkeypatch):
    """scores.bin reste valable quand seules les catégories dérivées changent ; une liste qui change le périme."""
    import shutil

    from app import precompute

    for path in precompute.DATA.iterdir():
        if path.suffix in (".txt", ".json"):
            shutil.copy(path, tmp_path / path.name)
    monkeypatch.setattr(precompute, "DATA", tmp_path)
    before = precompute.fingerprint()
    (tmp_path / wordlists.DERIVED_FILE).write_text("assurance\tjob,sport\n", encoding="utf-8")
    assert precompute.fingerprint() == before
    (tmp_path / "secteurs.txt").write_text("assurance\n", encoding="utf-8")
    assert precompute.fingerprint() != before


def test_explorer_filters_counts_map_and_order_see_derived_categories(env):
    env.fr._save_searched(wordlists.listed())  # import .fr fait avec toutes les listes : rien de masqué
    add_fr(env, "maison")
    u = env.universe
    i = u.index("plombierlyon")
    assert i in u.by_list["job"] and i in u.by_list["local"] and i in u.in_lists("job,sport")
    assert u.index("assurance") in u.by_list["premium"] and u.index("coachsportif") in u.by_list["sport"]
    found = env.search("fr", q="plombierlyon", mode="exact", category="job")["items"]
    assert [x["label"] for x in found] == ["plombierlyon"] and found[0]["categories"] == ["combo", "job", "local"]
    assert env.card("assuranceanimaux")["categories"] == ["animal", "premium", "keyword"]
    # Compteurs : l'union ; cat=sector se lit comme cat=premium (vues enregistrées, adresses anciennes).
    every = env.facets("fr", category="premium", junk=True)
    assert every == env.facets("fr", category="sector", junk=True) == env.facets("fr", category="premium,sector", junk=True)
    # Termes stars : ses 4 800 noms et les 747 secteurs et tendances qui n'y étaient pas (quelques noms de 2 lettres
    # réservés en .fr à part).
    assert len(wordlists.lists()["premium"]) + 700 < every["available"] <= len(wordlists.classes()["premium"])
    both = env.facets("fr", category="job,local", junk=True)["available"]
    assert len(wordlists.lists()["job"] | wordlists.lists()["local"]) + 14_000 < both
    # Carte : une ligne par catégorie affichée, même compte que la puce « Libres ».
    grid = env.map()
    assert "sector" not in grid["rows"] and grid["rows"]["media"] == "Noms de médias"
    assert grid["cells"]["fr"]["premium"] == env.facets("fr", category="premium")["bookable"]
    # Fréquence : un nom dérivé prend son rang dans la liste d'où vient la catégorie (assurance, 1er secteur
    # porteur ; ia, 1re tendance) ; un thème garde d'abord ses propres mots.
    assert env.facets("fr", category="premium", max_rank=1, junk=True)["available"] == 3
    sport = [x["label"] for x in env.rank("fr", category="sport", sort="freq", limit=300, junk=True)["items"]]
    own = wordlists.lists()["sport"]
    assert sport[0] == "sport" and sport.index("coachsportif") > max(sport.index(w) for w in own if w in sport)
    item = next(x for x in env.rank("fr", category="sport", sort="freq", limit=300, junk=True)["items"]
                if x["label"] == "coachsportif")
    assert item["freq_list"] == "keyword" and "sport" not in item["ranks"]
    # Liste principale des comparables : la liste d'origine, pas une catégorie dérivée.
    assert env._family(i, "plombierlyon") == "combo"


def test_routes_show_derived_categories(logged):
    client = logged()
    conn = client.app.state.conn
    conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('plombierlyon', '2010-02-01'), ('assurance', '2001-01-01')")
    conn.execute("INSERT INTO drops (name, tld, label, kind, removed_on, state, lists) VALUES "
                 "('assurance.com', 'com', 'assurance', 'long', '2026-10-01', 'redemption', 'word,english,sector'), "
                 "('zzqvxold.com', 'com', 'zzqvxold', 'long', '2026-10-01', 'redemption', 'sector,premium')")
    try:
        # Veille : les catégories du jour ; un nom sorti des listes garde les siennes, secteur lu comme premium.
        items = {i["name"]: i for i in client.get("/api/drops").json()["items"]}
        assert items["assurance.com"]["categories"] == ["word", "english", "premium"]
        assert items["zzqvxold.com"]["categories"] == ["premium"]
        data = client.get("/api/explore", params={"q": "plombierlyon", "mode": "exact", "cat": "job"}).json()
        assert data["items"][0]["categories"] == ["combo", "job", "local"]
        assert "sector" not in data["kinds"] and data["kinds"]["media"] == "Noms de médias"
        old = client.get("/api/explore", params={"q": "assurance", "mode": "exact", "cat": "sector"})
        assert old.status_code == 200 and old.json()["items"][0]["name"] == "assurance.fr"
        csv = client.get("/api/explore/export", params={"q": "plombierlyon", "mode": "exact", "cat": "local",
                                                        "status": "registered"}).text
        assert "plombierlyon.fr;Enregistré;" in csv and ";combo,job,local;" in csv
        fiche = client.get("/api/explore/fiche", params={"name": "assurance.fr"}).json()
        assert fiche["categories"] == ["word", "english", "premium"]
    finally:
        client.__exit__(None, None, None)
