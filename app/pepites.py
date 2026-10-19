"""Pépites : les noms de grande valeur qu'on peut acheter, ou bientôt.

Une pépite : un avis IA « À prendre » avec une note de revente au moins égale au seuil (Paramètres, carte Alertes),
sur un nom libre (vérifié au registre ou chez Gandi ; libre selon le WHOIS, à confirmer, compris mais signalé ;
premium vu chez Gandi, au prix affiché) ou bientôt libre (expiré en délai de grâce, rédemption, suppression
imminente). Toutes les extensions, qu'il sorte d'une zone (Veille) ou n'ait jamais été enregistré (Explorer) :
l'état est celui de l'Explorer (status_bytes, règle unique explore.gandi_effect), jamais recalculé ici.

Table pepites : une ligne par nom vu pépite, pour dater l'événement (devenue libre, devenue bientôt libre) et ne
jamais prévenir deux fois, redémarrage ou rattrapage compris. Un nom repris depuis plus de 7 jours repart de zéro :
sa prochaine libération est un nouvel événement.
"""

from datetime import datetime, timedelta

from . import ai, drops, explore, gandi, states
from .checker import iso, parse_iso

FREE, UNCONFIRMED, PREMIUM, COMING, GONE, TAKEN = "free", "unconfirmed", "premium", "coming", "gone", "taken"
OPEN = (FREE, UNCONFIRMED, PREMIUM)
TAKE = "À prendre"
DEFAULT_MIN = 70
# Événement déjà vieux quand il est vu (seuil baissé après coup, par exemple) : pas d'alerte immédiate, le nom
# reste dans Aujourd'hui et l'Explorer.
WINDOW = timedelta(hours=48)
# Nom tout juste libre au registre : son prix est demandé à Gandi une minute après (drops.on_freed) ; l'alerte attend.
PRICE_WAIT = timedelta(minutes=5)
# Quotidien : une pépite bientôt libre est rappelée une fois quand sa libération tombe sous 48 h.
SOON = timedelta(hours=48)
# Repris moins longtemps (lecture DNS contredite par le registre) : le même événement, pas de nouvelle alerte.
CYCLE = timedelta(days=7)
MAX_LINES = 10


def _verdict(comment: str | None) -> str:
    return (comment or "").split(".")[0]


def _chunks(conn, sql: str, names: list[str]) -> dict:
    found = {}
    for start in range(0, len(names), 900):
        part = names[start:start + 900]
        found.update({r["name"]: r for r in conn.execute(sql.format(",".join("?" * len(part))), part)})
    return found


def scan(explorer, now: datetime, minimum: int) -> dict[str, dict]:
    """Les noms notés « À prendre » au seuil, avec leur état de pépite : free, unconfirmed, premium, coming ;
    taken (enregistré ou suspendu) ; None (pas achetable : probablement libre non vérifié, réservé, indisponible
    à l'achat, premium probable). Les noms ignorés (Veille, « Ignorer ») n'en sont pas."""
    conn = explorer.conn
    rows = conn.execute("SELECT * FROM domain_ai WHERE resale >= ? AND comment LIKE ?", (minimum, TAKE + "%")).fetchall()
    hidden = {r[0] for r in conn.execute("SELECT name FROM drop_hidden")}
    opinions = {r["name"]: ai.public(r) for r in rows if _verdict(r["comment"]) == TAKE and r["name"] not in hidden}
    names = sorted(opinions)
    dropped = _chunks(conn, "SELECT * FROM drops WHERE name IN ({})", names)
    checks = gandi.load(conn, names)
    listed = explorer.reserved.names() if explorer.reserved is not None else frozenset()
    tlds, u = set(explorer.tlds()), explorer.universe
    data: dict[str, bytearray] = {}
    found = {}
    for name in names:
        label, _, tld = name.partition(".")
        i = u.index(label)
        if tld in tlds and i is not None:
            if tld not in data:
                data[tld] = explorer.status_bytes(tld)
            code = data[tld][i]
        else:
            code = explorer._describe_unknown(name, now)["status"]
        drop, check = dropped.get(name), checks.get(name)
        since, checked = (drop["state_since"] if drop else None), gandi.registry_checked(drop)
        kind = check["kind"] if explore.Explorer._gandi_seen(code, check, since, name in listed, checked) else None
        state = {explore.FREE: FREE, explore.UNCONFIRMED: UNCONFIRMED, explore.COMING: COMING,
                 explore.REGISTERED: TAKEN, explore.SUSPENDED: TAKEN}.get(code)
        if code == explore.PREMIUM and kind == "premium":
            state = PREMIUM
        opinion = opinions[name]
        rated = max(opinion["rated_at"] or "", opinion.get("reviewed_at") or "")
        registry_free = bool(drop and drop["state"] == states.AVAILABLE and drop["available_since"])
        if state in OPEN:
            # Devenue libre : libération constatée au registre, sinon premier constat (WHOIS), sinon réponse de Gandi.
            freed = (drop["available_since"] if registry_free
                     else drop["state_since"] if drop and drop["state"] == drops.UNCONFIRMED and drop["state_since"]
                     else check["checked_at"] if kind else (drop["state_since"] if drop else None)) or iso(now)
        elif state == COMING:
            freed = (drop["state_since"] if drop else None) or iso(now)
        else:
            freed = None
        found[name] = {
            "name": name, "tld": tld, "state": state, "code": code, "gandi_kind": kind,
            "registry": registry_free, "drop_state": drop["state"] if drop else None,
            # L'événement : le nom devient pépite quand il est libre (ou bientôt) et noté « À prendre ».
            "since": max(freed, rated) if freed else None,
            "release_at": drop["release_at"] if state == COMING and drop else None,
            "price": (round(check["price"]) if kind in ("available", "premium") and check["price"] is not None else None),
            # Indisponible chez Gandi, registre pas encore revu : la revérification tranche avant toute alerte.
            "pending": gandi.pending(check, checked),
            "price_known": bool(kind) or bool(check and registry_free and check["checked_at"] >= drop["available_since"]),
            "ai": opinion,
        }
    return found


def sync(conn, found: dict[str, dict], now: datetime) -> None:
    """Range le passage dans la table : nouvelles pépites datées, état à jour, repris effacés après CYCLE.
    Premier passage (référence) : les pépites libres déjà là sont marquées prévenues, sans message."""
    stamp = iso(now)
    known = {r["name"]: r for r in conn.execute("SELECT * FROM pepites")}
    for name, p in found.items():
        row, state = known.get(name), p["state"]
        if state == TAKEN:
            if row is not None and row["state"] != TAKEN:
                conn.execute("UPDATE pepites SET state = ?, taken_at = ?, seen_at = ? WHERE name = ?", (TAKEN, stamp, stamp, name))
            continue
        if state is None:
            if row is not None and row["state"] not in (GONE, TAKEN):
                conn.execute("UPDATE pepites SET state = ?, seen_at = ? WHERE name = ?", (GONE, stamp, name))
            continue
        if row is not None and row["state"] == TAKEN and (parse_iso(row["taken_at"]) or now) <= now - CYCLE:
            conn.execute("DELETE FROM pepites WHERE name = ?", (name,))  # nouveau cycle : tout repart de zéro
            row = None
        if row is None:
            conn.execute(
                "INSERT INTO pepites (name, state, free_at, coming_at, release_at, seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, state, p["since"] if state in OPEN else None, p["since"] if state == COMING else None,
                 p["release_at"], stamp))
        else:
            # Bientôt libre puis libre : l'événement « libre » prend sa date à ce moment-là.
            conn.execute(
                "UPDATE pepites SET state = ?, free_at = COALESCE(free_at, ?), coming_at = COALESCE(coming_at, ?), "
                "release_at = ?, seen_at = ?, taken_at = NULL WHERE name = ?",
                (state, p["since"] if state in OPEN else None, p["since"] if state == COMING else None,
                 p["release_at"], stamp, name))
    gone = [n for n, r in known.items() if n not in found and r["state"] not in (GONE, TAKEN)]
    conn.executemany("UPDATE pepites SET state = ?, seen_at = ? WHERE name = ?", [(GONE, stamp, n) for n in gone])
    from .store import Store

    store = Store(conn)
    if not store.get("pepites_baseline"):
        conn.execute("UPDATE pepites SET alerted_at = ? WHERE state IN (?, ?, ?) AND alerted_at IS NULL", (stamp, *OPEN))
        store.set("pepites_baseline", stamp)


def rows(conn) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in conn.execute("SELECT * FROM pepites")}


def current(conn, free: bool = True, coming: bool = True) -> set[str]:
    """Noms couverts par les alertes de pépites (libres, bientôt libres) : les autres messages ne les répètent pas."""
    wanted = (OPEN if free else ()) + ((COMING,) if coming else ())
    if not wanted:
        return set()
    return {r[0] for r in conn.execute(f"SELECT name FROM pepites WHERE state IN ({','.join('?' * len(wanted))})", wanted)}


def due_free(conn, found: dict[str, dict], now: datetime, watched: set[str], gandi_on: bool) -> tuple[list[dict], list[str]]:
    """Alerte immédiate : (pépites libres à annoncer, noms à marquer sans message). Un nom suivi dans Domaines a déjà
    son alerte de changement d'état ; un événement plus vieux que WINDOW n'est plus une nouvelle. En attente : Gandi
    l'a dit indisponible (revérification en cours), ou son prix arrive dans la minute."""
    send, silent = [], []
    for name, row in rows(conn).items():
        p = found.get(name)
        if row["state"] not in OPEN or row["alerted_at"] or p is None or p["state"] not in OPEN:
            continue
        if p["pending"]:
            continue
        if name in watched or (parse_iso(row["free_at"]) or now) < now - WINDOW:
            silent.append(name)
            continue
        if gandi_on and p["registry"] and not p["price_known"] and (parse_iso(row["free_at"]) or now) > now - PRICE_WAIT:
            continue
        send.append(p)
    return order(send), silent


def mark_alerted(conn, names: list[str], now: datetime) -> None:
    conn.executemany("UPDATE pepites SET alerted_at = ? WHERE name = ?", [(iso(now), n) for n in names])


def order(items: list[dict]) -> list[dict]:
    """Par note de revente, puis prix utilisateur final, puis nom."""
    return sorted(items, key=lambda p: (-p["ai"]["resale"], -(p["ai"]["final_high"] or 0), p["name"]))


# --- Messages -------------------------------------------------------------------------

def euros(value) -> str:
    return f"{int(value):,} €".replace(",", " ")


def price_range(low, high) -> str:
    if high is None:
        return "non estimé"
    return euros(high) if low is None or low == high else f"{euros(low).removesuffix(' €')} à {euros(high)}"


def _day(value: str | None) -> str:
    from .digest import local_zone  # import local : digest importe checker

    found = parse_iso(value)
    return found.astimezone(local_zone()).strftime("%d/%m") if found else ""


def state_text(p: dict) -> str:
    if p["state"] == FREE:
        return "libre, vérifié au registre" if p["registry"] else "libre, vérifié chez Gandi"
    if p["state"] == UNCONFIRMED:
        return "libre selon le WHOIS, à confirmer chez un registrar"
    if p["state"] == PREMIUM:
        return "premium chez Gandi"
    label = explore.DROP_LABELS.get(p["drop_state"], "bientôt libre").lower()
    day = _day(p.get("release_at"))
    return f"{label}, libération ~{day}" if day else f"{label}, libération non estimée"


def block(p: dict, base: str, order_url: str, title: str = "", note: str = "") -> list[str]:
    """Gabarit commun : quoi et nom, état, prix d'achat, avis IA, raison, liens."""
    from .morning import link

    o = p["ai"]
    lines = [f"{title} : {p['name']}" if title else p["name"], "État : " + state_text(p) + (f" ({note})" if note else "")]
    if p.get("price") is not None and p["state"] in OPEN:
        lines.append(f"Prix d'achat : {euros(p['price'])} chez Gandi" + (" (premium)" if p["state"] == PREMIUM else ""))
    lines.append(f"Avis IA : {TAKE}, revente {o['resale']}, exploitation {o['exploit']}, "
                 f"prix final {price_range(o['final_low'], o['final_high'])}")
    reason = (o.get("comment") or "").partition(". ")[2]
    if reason:
        lines.append(reason)
    url = link(base, p["name"])
    if url:
        lines.append("Fiche : " + url)
    if order_url and p["state"] in OPEN:
        lines.append("Réserver : " + order_url.replace("{domain}", p["name"]))
    return lines


def free_message(items: list[dict], base: str, order_url: str) -> str | None:
    """Une pépite : son bloc ; plusieurs du même passage : un seul message, 10 détaillées puis « et N autres »."""
    if not items:
        return None
    if len(items) == 1:
        return "\n".join(block(items[0], base, order_url, "Pépite libre"))
    lines = [f"Pépites libres : {len(items)} noms"]
    for p in items[:MAX_LINES]:
        lines += [""] + block(p, base, order_url)
    if len(items) > MAX_LINES:
        lines += ["", f"et {len(items) - MAX_LINES} autres, dans Aujourd'hui"]
    return "\n".join(lines)


def daily_pick(conn, found: dict[str, dict], now: datetime) -> list[tuple[dict, str]]:
    """Pépites bientôt libres pour le quotidien : les nouvelles, celles dont la libération tombe sous 48 h (une
    fois), celles dont la date estimée a changé. Jamais la même liste chaque jour."""
    picks = []
    for name, row in rows(conn).items():
        p = found.get(name)
        if row["state"] != COMING or p is None or p["state"] != COMING:
            continue
        release = parse_iso(p["release_at"])
        if not row["daily_at"]:
            note = "nouvelle"
        elif release and release - now <= SOON and not row["daily_soon"]:
            note = "libération sous 48 h"
        elif release and row["daily_release"] and _day(row["daily_release"]) != _day(p["release_at"]):
            note = "date revue"
        else:
            continue
        picks.append((p, note))
    return sorted(picks, key=lambda pair: (-pair[0]["ai"]["resale"], -(pair[0]["ai"]["final_high"] or 0), pair[0]["name"]))


def daily_section(picks: list[tuple[dict, str]], base: str) -> str | None:
    if not picks:
        return None
    lines = [f"Pépites bientôt libres : {len(picks)}"]
    for p, note in picks[:MAX_LINES]:
        lines += [""] + block(p, base, "", note=note)
    if len(picks) > MAX_LINES:
        lines += ["", f"et {len(picks) - MAX_LINES} autres, dans Aujourd'hui"]
    return "\n".join(lines)


def mark_daily(conn, picks: list[tuple[dict, str]], now: datetime) -> None:
    for p, _ in picks:
        release = parse_iso(p["release_at"])
        soon = bool(release and release - now <= SOON)
        conn.execute("UPDATE pepites SET daily_at = ?, daily_release = ?, daily_soon = MAX(daily_soon, ?) WHERE name = ?",
                     (iso(now), p["release_at"], int(soon), p["name"]))


# --- Aujourd'hui ----------------------------------------------------------------------

def today(conn, found: dict[str, dict], now: datetime, hours: int, nopremium: bool = False) -> dict[str, list[dict]]:
    """Rubrique Pépites d'Aujourd'hui pour la période : devenues libres depuis (libération constatée, première
    confirmation de libre, ou avis reçu sur un nom libre), libération estimée dans la période. Même calcul pour
    le compteur et pour la liste. La date est celle de la table une fois le nom vu, sinon celle du passage."""
    since, soon = now - timedelta(hours=hours), now + timedelta(hours=hours)
    known = rows(conn)
    free, coming = [], []
    for name, p in found.items():
        if nopremium and p["state"] == PREMIUM:
            continue
        if p["state"] in OPEN:
            at = parse_iso((known.get(name) or {}).get("free_at") or p["since"])
            if at and at >= since:
                free.append({**p, "since": iso(at)})
        elif p["state"] == COMING:
            release = parse_iso(p["release_at"])
            if release and release <= soon:
                coming.append(p)
    return {"free": order(free), "coming": order(coming)}


def items(explorer, found: list[dict], now: datetime) -> list[dict]:
    """Éléments de carte (format de l'Explorer : état, gandi, gandi_state, score), dans l'ordre donné."""
    if not found:
        return []
    u, tlds = explorer.universe, set(explorer.tlds())
    by_tld: dict[str, list[int]] = {}
    for p in found:
        label, _, tld = p["name"].partition(".")
        i = u.index(label)
        if tld in tlds and i is not None:
            by_tld.setdefault(tld, []).append(i)
    described = {}
    for tld, indices in by_tld.items():
        described.update({d["name"]: d for d in explorer.describe(tld, indices)})
    values = explorer.value_of([p["name"] for p in found if p["name"] in described])
    out = []
    for p in found:
        item = described.get(p["name"])
        item = {**item, **values[p["name"]]} if item else explorer._describe_unknown(p["name"], now)
        item["pepite"] = {"state": p["state"], "since": p["since"], "release_at": p["release_at"], "price": p["price"]}
        out.append(item)
    return out
