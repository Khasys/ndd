"""Tableau de référence de l'état d'un nom : moment de sa vie × réponses des sources → état affiché.

Une seule règle pour l'effet de Gandi (explore.gandi_effect), une seule pour la Veille face au DNS
(explore.drop_open_code) ; et le même état dans tous les écrans : Explorer et fiche, Veille, Domaines.
"""

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app import drops, explore, gandi, states
from app.checker import iso
from app.rdap import DomainInfo, LookupResult
from tests.test_app import client_factory, logged  # noqa: F401  (fixtures)
from tests.test_gandi import _com_zone

E = explore
CODES = (E.INVALID, E.REGISTERED, E.GONE, E.ABSENT, E.COMING, E.FREE, E.SUSPENDED, E.RESERVED, E.PREMIUM, E.UNCONFIRMED)
KINDS = ("available", "premium", "unavailable", "reserved", "restricted", "error")
AT = "2026-10-19T03:00:00+00:00"  # réponse de Gandi
BEFORE, AFTER = "2026-10-18T20:00:00+00:00", "2026-10-19T06:00:00+00:00"


# --- La règle de Gandi, toutes les combinaisons -------------------------------------------------------

@pytest.mark.parametrize("code,kind,since,checked,listed", itertools.product(
    CODES, KINDS, (None, BEFORE, AFTER), (None, "", BEFORE, AFTER), (False, True)))
def test_gandi_effect_table(code, kind, since, checked, listed):
    check = {"kind": kind, "checked_at": AT}
    effect, applied = E.gandi_effect(code, check, since, checked, listed)
    stale = since == AFTER  # état changé après la réponse : elle attend la passe suivante
    waiting = kind in gandi.BLOCKED and checked is not None and checked < AT  # registre pas revu depuis
    untouchable = code not in E.GANDI_OPEN or (code == E.RESERVED and listed)
    if kind == "error" or stale or waiting or untouchable:
        assert (effect, applied) == (code, None)
    else:
        assert effect == {"available": E.FREE, "premium": E.PREMIUM}.get(kind, E.RESERVED) and applied == kind
    # Un nom enregistré, bientôt libre ou suspendu ne change jamais, quoi que dise Gandi.
    if code in (E.REGISTERED, E.COMING, E.SUSPENDED, E.INVALID):
        assert effect == code
    # Jamais « libre » à cause d'une réponse qui dit qu'on ne peut pas acheter.
    if kind in gandi.BLOCKED:
        assert effect != E.FREE or code == E.FREE


def test_gandi_effect_without_answer_changes_nothing():
    for code in CODES:
        assert E.gandi_effect(code, None) == (code, None)


@pytest.mark.parametrize("state,checked,delegated,expected", [
    (states.AVAILABLE, BEFORE, AFTER, E.REGISTERED),   # libre au registre, puis vu délégué : enregistré depuis
    (states.AVAILABLE, AFTER, BEFORE, E.FREE),         # délégation plus ancienne que la lecture du registre
    (states.AVAILABLE, None, AT, E.REGISTERED),
    (drops.UNCONFIRMED, BEFORE, AFTER, E.REGISTERED),
    (states.REDEMPTION, BEFORE, AFTER, E.COMING),      # en fin de vie, le nom reste délégué un temps
    (drops.TAKEN, BEFORE, None, E.REGISTERED),
    (states.RENEWED, BEFORE, None, E.REGISTERED),
    (drops.SUSPENDED, BEFORE, AFTER, E.SUSPENDED),
    (None, None, AFTER, None),
])
def test_drop_open_code_latest_observation_wins(state, checked, delegated, expected):
    assert E.drop_open_code(state, checked, delegated) == expected


def test_unavailable_premium_is_not_buyable():
    """menu.co, pris : Gandi répond unavailable_premium. Un premium enregistré ne s'achète pas : indisponible."""
    raw = {"currency": "EUR", "products": [{"name": "menu.co", "status": "unavailable_premium", "process": "create"}]}
    assert gandi.read(raw)["kind"] == "unavailable"
    free = {"currency": "EUR", "products": [{"name": "x.com", "status": "available", "process": "create",
                                             "prices": [{"type": "premium", "price_after_taxes": 878.4, "min_duration": 1}]}]}
    assert gandi.read(free)["kind"] == "premium"


# --- Le même état dans tous les écrans ---------------------------------------------------------------

def explorer_class(item):
    code, kind = item["status"], item.get("gandi_state")
    if code == E.RESERVED:
        return "blocked" if kind in gandi.BLOCKED else "reserved"
    return {E.REGISTERED: "taken", E.SUSPENDED: "taken", E.COMING: "coming", E.FREE: "free", E.PREMIUM: "premium",
            E.ABSENT: "maybe", E.GONE: "maybe", E.UNCONFIRMED: "maybe"}[code]


def veille_class(item):
    kind = item.get("gandi_state")
    if item["state"] in drops.TRACKED:
        return "coming"
    if item["state"] in (drops.TAKEN, states.RENEWED):
        return "taken"
    if kind in gandi.BLOCKED:
        return "blocked"
    if kind == "premium":
        return "premium"
    if item["reserved"]:
        return "reserved"
    if item["state"] == states.AVAILABLE or kind == "available":
        return "free"
    return "maybe"


def domain_class(d):
    if d["state"] in (states.REDEMPTION, states.PENDING_DELETE, states.EXPIRED):
        return "coming"
    if d["state"] != states.AVAILABLE:
        return "taken"
    return {"premium": "premium"}.get(d.get("gandi_state"), "blocked" if d.get("gandi_state") in gandi.BLOCKED else "free")


# nom : (dans la zone, Veille (état, entrée dans l'état, lu au registre), Gandi (sorte, à), suivi (état, entrée, lu), attendu)
SCENARIOS = {
    "maison": (True, None, None, None, "taken"),
    "velo": (False, (states.REDEMPTION, BEFORE, BEFORE), ("unavailable", AT), None, "coming"),
    "chat": (False, (states.REDEMPTION, BEFORE, BEFORE), ("premium", AT), None, "coming"),
    "lune": (False, (states.AVAILABLE, BEFORE, BEFORE), ("available", AT), None, "free"),
    "ciel": (False, (states.AVAILABLE, BEFORE, BEFORE), ("premium", AT), None, "premium"),
    # Indisponible, registre pas revu depuis : l'état d'avant tient, la revérification tranche.
    "jardin": (False, (states.AVAILABLE, BEFORE, BEFORE), ("unavailable", AT), None, "free"),
    # Registre revu après : toujours libre au registre, refusé à l'achat.
    "plage": (False, (states.AVAILABLE, BEFORE, AFTER), ("unavailable", AT), None, "blocked"),
    # Changement d'état après la réponse : elle ne compte plus.
    "soleil": (False, (states.AVAILABLE, AFTER, AFTER), ("premium", AT), None, "free"),
    "voyage": (False, (drops.UNCONFIRMED, BEFORE, BEFORE), ("available", AT), None, "free"),
    "credit": (False, (drops.TAKEN, AFTER, AFTER), ("available", AT), None, "taken"),
    "mobile": (False, None, ("available", AT), None, "free"),
    "banque": (False, None, ("unavailable", AT), None, "blocked"),
    "running": (False, None, ("premium", AT), None, "premium"),
    # Suivi dans Domaines et dans la Veille : la lecture du registre la plus récente vaut pour les deux.
    "ecole": (False, (states.AVAILABLE, BEFORE, BEFORE), ("unavailable", AT), (states.AVAILABLE, BEFORE, BEFORE), "free"),
    "musique": (False, (states.AVAILABLE, BEFORE, AFTER), ("unavailable", AT), (states.AVAILABLE, BEFORE, AFTER), "blocked"),
    "sport": (False, None, ("premium", AT), (states.AVAILABLE, BEFORE, AFTER), "premium"),
    "maison2": (True, None, None, (states.REDEMPTION, BEFORE, AFTER), "coming"),
}


def test_every_screen_shows_the_same_state(logged):
    client = logged()
    st = client.app.state
    conn = st.conn
    st.store.set("reserved_mode", "show")  # la Veille montre aussi les réservés : tous les noms y sont comparés
    labels = [label for label in SCENARIOS if st.explorer.universe.index(label) is not None]
    assert len(labels) >= 12, "mots absents de l'univers : choisir d'autres noms"
    _com_zone(st, words=[label for label in labels if SCENARIOS[label][0]])
    for label in labels:
        _, drop, check, domain, _ = SCENARIOS[label]
        name = f"{label}.com"
        if drop:
            st.drops.record("com", [label], [], datetime.now(timezone.utc))
            conn.execute("UPDATE drops SET state = ?, state_since = ?, checked_at = ?, available_since = ?, next_check_at = NULL "
                         "WHERE name = ?", (drop[0], drop[1], drop[2], drop[1] if drop[0] == states.AVAILABLE else None, name))
        if check:
            conn.execute("INSERT INTO gandi_checks (name, status, kind, price, checked_at) VALUES (?, ?, ?, ?, ?)",
                         (name, check[0], check[0], 878.4 if check[0] == "premium" else 13.2 if check[0] == "available" else None,
                          check[1]))
        if domain:
            conn.execute("INSERT INTO domains (name, added_at, state, state_since, last_success_at) VALUES (?, ?, ?, ?, ?)",
                         (name, BEFORE, *domain))
        st.explorer.touch(name)
    st.explorer.invalidate()
    veille = {i["name"]: i for i in client.get("/api/drops").json()["items"]}
    watched = {d["name"]: d for d in client.get("/api/domains").json()}
    for label in labels:
        name, expected = f"{label}.com", SCENARIOS[label][4]
        fiche = client.get("/api/explore/fiche", params={"name": name}).json()
        seen = {"explorer": explorer_class(fiche["item"])}
        listed = client.get("/api/explore", params={"tld": "com", "q": label, "mode": "exact", "status": "all"}).json()["items"]
        if listed:
            seen["liste"] = explorer_class(listed[0])
        if name in veille:
            seen["veille"] = veille_class(veille[name])
        if name in watched:
            seen["domaines"] = domain_class(watched[name])
        assert set(seen.values()) == {expected}, (name, seen)
        # Jamais « libre depuis » ni prix d'achat sur un nom qu'on ne peut pas acheter au prix normal.
        if expected == "blocked":
            assert fiche["item"]["status_label"] == gandi.KIND_LABELS["unavailable"]
    client.__exit__(None, None, None)


def test_watched_domain_check_updates_its_veille_line(logged):
    """lesimpots.fr : suivi dans Domaines et libre dans la Veille ; la lecture de Domaines vaut pour la Veille."""
    client = logged()
    st = client.app.state
    conn = st.conn
    _com_zone(st)
    now = datetime.now(timezone.utc)
    st.drops.record("com", ["lune"], [], now)
    conn.execute("UPDATE drops SET state = 'available', state_since = ?, checked_at = ?, available_since = ? WHERE name = 'lune.com'",
                 (BEFORE, BEFORE, BEFORE))
    taken = LookupResult(found=True, info=DomainInfo(statuses=["active"], registered_at=now, expires_at=now + timedelta(days=365)))
    st.explorer.sync_registry("lune.com", taken)
    assert st.drops.row("lune.com")["state"] == drops.TAKEN
    assert client.get("/api/explore/fiche", params={"name": "lune.com"}).json()["item"]["status"] == E.REGISTERED
    client.__exit__(None, None, None)


# --- Les combinaisons réelles du NAS ---------------------------------------------------------------------

def _result_of(state: str, at: datetime) -> LookupResult:
    """Lecture du registre qui donne cet état (écran Domaines) : rejouée par Explorer.sync_registry."""
    if state == states.AVAILABLE:
        return LookupResult(available=True)
    statuses = {states.REDEMPTION: ["redemption period"], states.PENDING_DELETE: ["pending delete"],
                states.RENEWED: ["auto renew period"]}.get(state, ["active"])
    expires = at + timedelta(days=10 if state == states.EXPIRING else 300)
    return LookupResult(found=True, info=DomainInfo(statuses=statuses, expires_at=expires, registered_at=at - timedelta(days=900)))


def test_real_cases_from_the_nas_agree_everywhere(logged):
    """Chaque combinaison relevée sur le NAS (tests/fixtures/state_cases.json), rejouée sur un mot neutre en .com :
    l'Explorer, la liste, la Veille et Domaines disent la même chose, et rien n'est « libre » ni à acheter quand
    Gandi refuse l'achat et que le registre l'a revu depuis."""
    import json
    from pathlib import Path

    cases = json.loads((Path(__file__).parent / "fixtures" / "state_cases.json").read_text())["cases"]
    client = logged()
    st = client.app.state
    conn = st.conn
    st.store.set("reserved_mode", "show")
    _com_zone(st)
    u = st.explorer.universe
    # Mots de 6 lettres et plus, hors des listes de réservés : rien d'autre que le cas ne joue sur l'état.
    labels = iter(w for w in u.words if len(w) >= 6 and w.isalpha() and f"{w}.com" not in st.reserved.names())
    names = []
    for case in cases:
        name = f"{next(labels)}.com"
        names.append((name, case))
        g = case.get("gandi")
        if g and g.get("kind"):
            conn.execute("INSERT INTO gandi_checks (name, status, kind, price, checked_at) VALUES (?, ?, ?, ?, ?)",
                         (name, g["status"] or g["kind"], g["kind"], g["price"], g["checked_at"]))
        if case["source"] == "veille":
            st.drops.record("com", [name[:-4]], [], datetime.now(timezone.utc))
            conn.execute("UPDATE drops SET state = ?, state_since = ?, checked_at = ?, available_since = ?, release_at = ?, "
                         "next_check_at = NULL WHERE name = ?",
                         (case["state"], case["state_since"], case["checked_at"], case["available_since"], case["release_at"], name))
        elif case["source"] == "domaines":
            at = datetime.fromisoformat(case["last_success_at"]) if case["last_success_at"] else datetime.now(timezone.utc)
            if case["state"] == states.RENEWED:
                case = {**case, "state": states.EXPIRED}  # « Renouvelé » est propre au .fr : en .com, la même lecture dit « expiré »
            conn.execute("INSERT INTO domains (name, added_at, state, state_since, last_success_at) VALUES (?, ?, ?, ?, ?)",
                         (name, BEFORE, case["state"], case["state_since"], case["last_success_at"]))
            if case["state"]:
                st.explorer.sync_registry(name, _result_of(case["state"], at), at)
        st.explorer.touch(name)
    st.explorer.invalidate()
    veille = {i["name"]: i for i in client.get("/api/drops").json()["items"]}
    watched = {d["name"]: d for d in client.get("/api/domains").json()}
    problems = []
    for name, case in names:
        item = client.get("/api/explore/fiche", params={"name": name}).json()["item"]
        seen = {"explorer": explorer_class(item)}
        if name in veille:
            seen["veille"] = veille_class(veille[name])
        if name in watched and watched[name]["state"]:
            seen["domaines"] = domain_class(watched[name])
        if len(set(seen.values())) != 1:
            problems.append((case, seen))
        g = item.get("gandi") or {}
        # Refusé à l'achat et registre revu depuis : jamais libre, jamais de prix d'achat affiché.
        if g.get("kind") in gandi.BLOCKED and not g.get("pending") and not g.get("stale") and item["status"] == E.FREE:
            problems.append((case, "libre malgré un refus d'achat"))
        if item.get("gandi_state") in gandi.BLOCKED and item["status"] != E.RESERVED:
            problems.append((case, "refus d'achat sans l'état"))
    assert not problems, problems
    client.__exit__(None, None, None)

