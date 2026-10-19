"""Alertes Telegram : un seul schéma, deux rythmes, une case par type (Paramètres, carte Alertes).

Immédiat :
- pépite libre (pepites.py) : vue chaque minute, une fois par nom et par libération, les pépites d'un même passage
  dans un seul message ;
- domaine suivi (écran Domaines) : changement d'état, de registrar ou de libération estimée (checker.py) ;
- analyse IA demandée prête.
Quotidien, un seul message (en plusieurs parts au-delà de 4 000 caractères) à l'heure réglée, 8 h par défaut :
pépites bientôt libres (nouvelles, libération sous 48 h ou date revue), bons noms des dernières 24 h (morning.py),
vues enregistrées en alerte, rappels de renouvellement du portefeuille, santé (morning.py), et le jour choisi le
résumé de la semaine des domaines suivis (digest.py).

Un nom annoncé comme pépite ne repart ni dans les bons noms du jour ni dans une vue. Chaque envoi est noté en base
une fois parti (ou mis en file de rejeu) : rien ne repart au redémarrage ni au rattrapage d'une tâche manquée.
"""

import json
from datetime import datetime, timedelta

from . import digest, morning, pepites, portfolio, states
from .checker import iso, parse_iso
from .morning import link

IMMEDIATE, DAILY, WEEKLY = "immediate", "daily", "weekly"
RHYTHMS = {IMMEDIATE: "Immédiat", DAILY: "Chaque jour", WEEKLY: "Chaque semaine, dans le quotidien"}
TYPES = (
    ("pepite_free", "Pépite libre", IMMEDIATE,
     "Nom « À prendre » au seuil, libre ou qui passe libre : état, prix d'achat, avis IA, lien vers la fiche et Réserver"),
    ("domain_change", "Domaine suivi", IMMEDIATE,
     "Nom de l'écran Domaines : changement d'état, de registrar ou de libération estimée"),
    ("ai_ready", "Analyse IA prête", IMMEDIATE, "Analyse demandée depuis une fiche, avec le lien"),
    ("pepite_coming", "Pépites bientôt libres", DAILY,
     "Expirées, en rédemption ou en suppression, avec la libération estimée : nouvelles, sous 48 h ou date revue"),
    ("morning", "Bons noms du jour", DAILY,
     "Libérés, entrés en rédemption, à confirmer, sortis du DNS en 24 h : score minimum ou avis favorable"),
    ("views", "Vues enregistrées", DAILY, "Nouveaux noms des vues marquées « alerte » (Explorer)"),
    ("renewals", "Renouvellements", DAILY, "Noms du portefeuille, 30 puis 7 jours avant l'échéance"),
    ("health", "Santé", DAILY, "Pannes apparues ou réglées : zones, .fr, DNS, Gandi, notation IA, planificateur"),
    ("weekly", "Résumé de la semaine", WEEKLY,
     "Domaines suivis : changements, expirations et libérations à 30 jours, erreurs ; le jour choisi"),
)
KEYS = tuple(t[0] for t in TYPES)
DEFAULT_HOUR = 8
DEFAULT_MORNING_MIN = 40
MAX_TEXT = 4000
MAX_LINES = 10


def enabled(store, key: str) -> bool:
    return store.get(f"alert_on_{key}", "1") == "1"


def _int(store, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(store.get(key) or default)))
    except ValueError:
        return default


def pepite_min(store) -> int:
    return _int(store, "alert_pepite_min", pepites.DEFAULT_MIN, 0, 100)


def daily_hour(store) -> int:
    return _int(store, "alert_daily_hour", DEFAULT_HOUR, 0, 23)


def weekly_day(store) -> int:
    return _int(store, "alert_weekly_day", 0, 0, 6)


def morning_min(store) -> int:
    return _int(store, "alert_morning_min", DEFAULT_MORNING_MIN, 0, 100)


def settings(store) -> dict:
    """Carte Alertes : chaque type avec son rythme et sa case, le seuil des pépites, l'heure du quotidien."""
    return {
        "types": [{"key": k, "label": label, "rhythm": rhythm, "rhythm_label": RHYTHMS[rhythm], "text": text,
                   "enabled": enabled(store, k)} for k, label, rhythm, text in TYPES],
        "pepite_min": pepite_min(store),
        "daily_hour": daily_hour(store),
        "weekly_day": weekly_day(store),
        "morning_min": morning_min(store),
        "daily_last": store.get("alert_daily_last") or None,
    }


def save(store, enabled_types: dict[str, bool] | None = None, **values) -> None:
    for key, on in (enabled_types or {}).items():
        if key in KEYS:
            store.set(f"alert_on_{key}", "1" if on else "0")
    for key in ("pepite_min", "daily_hour", "weekly_day", "morning_min"):
        if values.get(key) is not None:
            store.set(f"alert_{key}", str(int(values[key])))


# --- Immédiat : pépites libres ---------------------------------------------------------------

def pepite_tick(explorer, store, now: datetime, base: str, order_url: str, can_send: bool, gandi_on: bool) -> tuple[str | None, list[str]]:
    """Un passage : pépites relues et rangées (pepites.sync), puis le message des pépites libres à annoncer et
    leurs noms (l'appelant les marque une fois le message parti). Type désactivé ou Telegram pas configuré :
    les nouvelles pépites sont marquées sans message, rien ne part en rafale au réglage suivant."""
    conn = explorer.conn
    found = pepites.scan(explorer, now, pepite_min(store))
    pepites.sync(conn, found, now)
    watched = {r[0] for r in conn.execute("SELECT name FROM domains")}
    send, silent = pepites.due_free(conn, found, now, watched, gandi_on)
    if not (can_send and enabled(store, "pepite_free")):
        silent, send = silent + [p["name"] for p in send], []
    pepites.mark_alerted(conn, silent, now)
    return pepites.free_message(send, base, order_url), [p["name"] for p in send]


# --- Vues enregistrées ---------------------------------------------------------------------

def changed(conn, since: datetime) -> list[dict]:
    """Noms de la Veille passés libres ou entrés en rédemption (ou en suppression) depuis `since`, les plus récents d'abord."""
    stamp = iso(since)
    coming = (states.REDEMPTION, states.PENDING_DELETE)
    rows = conn.execute(
        f"""SELECT name, state, state_since, available_since, release_at FROM drops
            WHERE ((state = ? AND available_since >= ?)
               OR (state IN ({','.join('?' * len(coming))}) AND state_since >= ?)) AND origin IS NOT 'gandi'
            ORDER BY COALESCE(available_since, state_since) DESC""",
        (states.AVAILABLE, stamp, *coming, stamp),
    ).fetchall()
    return [{"name": r["name"], "free": r["state"] == states.AVAILABLE, "release_at": r["release_at"]} for r in rows]


def matches(explorer, params: dict, name: str) -> bool:
    """Le nom répond aux filtres de la vue : même classement que l'Explorer, sur ce seul nom."""
    label, _, tld = name.partition(".")
    if tld not in params["tlds"]:
        return False
    rest = {k: v for k, v in params.items() if k not in ("tld", "tlds", "cursor", "q", "mode")}
    found = explorer.rank(tld, q=label, mode="exact", limit=1, **rest)["items"]
    return bool(found) and found[0]["name"] == name


def _short_state(item: dict) -> str:
    if item["free"]:
        return "libre"
    release = parse_iso(item.get("release_at"))
    return f"libre ~{release.astimezone(digest.local_zone()).strftime('%d/%m')}" if release else "bientôt libre"


def view_sections(conn, explorer, store, base: str, now: datetime, to_params, exclude: set[str]) -> tuple[list[str], list[str]]:
    """Une rubrique par vue en alerte qui a du nouveau depuis son dernier quotidien, et les vues lues (notées une fois
    le message parti). `to_params(filters)` traduit les filtres enregistrés comme ceux de l'adresse de l'Explorer
    (None : filtres invalides). exclude : noms déjà annoncés comme pépites."""
    last = json.loads(store.get("view_alert_last") or "{}")
    out, seen = [], []
    for view in json.loads(store.get("explore_views") or "[]"):
        if not view.get("alert"):
            continue
        # Vue sans extension : toutes ; sans état ni niveau : les réglages simples de l'Explorer.
        filters = {"status": "bookable", "level": "good", **(view.get("filters") or {})}
        params = to_params(filters)
        if params is None:
            continue
        if "tld" not in filters:
            params["tlds"] = explorer.tlds()
        seen.append(view["name"])
        since = parse_iso(last.get(view["name"])) or now - timedelta(hours=24)
        found = [c for c in changed(conn, since) if c["name"] not in exclude and matches(explorer, params, c["name"])]
        if not found:
            continue
        lines = [f"Vue « {view['name']} » : {len(found)} nouveau{'x' if len(found) > 1 else ''} nom{'s' if len(found) > 1 else ''}"]
        for c in found[:MAX_LINES]:
            lines.append(f"{c['name']} : {_short_state(c)}")
            url = link(base, c["name"])
            if url:
                lines.append("Fiche : " + url)
        if len(found) > MAX_LINES:
            lines.append(f"et {len(found) - MAX_LINES} autres")
        out.append("\n".join(lines))
    return out, seen


def mark_views(store, names: list[str], now: datetime) -> None:
    last = json.loads(store.get("view_alert_last") or "{}")
    last.update({n: iso(now) for n in names})
    store.set("view_alert_last", json.dumps(last))


# --- Quotidien -----------------------------------------------------------------------------

def daily_due(store, now_local: datetime) -> bool:
    """Une fois par jour, dès l'heure réglée (rattrapé plus tard si l'appli était arrêtée à cette heure-là)."""
    return now_local.hour >= daily_hour(store) and store.get("alert_daily_last") != now_local.date().isoformat()


def daily(explorer, store, checker, cc_status: dict, base: str, now: datetime, now_local: datetime,
          to_params) -> tuple[list[str], dict]:
    """Le quotidien : ses messages (vide : rien à dire) et ce qu'il couvre, à noter par commit_daily une fois parti."""
    conn = explorer.conn
    marks: dict = {"day": now_local.date().isoformat()}
    parts: list[str] = []
    found = pepites.scan(explorer, now, pepite_min(store))
    pepites.sync(conn, found, now)
    # Pépites annoncées par leur propre type (immédiat ou quotidien) : pas répétées dans les bons noms ni les vues.
    covered = pepites.current(conn, free=enabled(store, "pepite_free"), coming=enabled(store, "pepite_coming"))
    if enabled(store, "pepite_coming"):
        marks["pepites"] = pepites.daily_pick(conn, found, now)
        section = pepites.daily_section(marks["pepites"], base)
        if section:
            parts.append(section)
    if enabled(store, "morning"):
        parts += morning.sections(conn, explorer, cc_status, base, now, morning_min(store), covered)
    if enabled(store, "views"):
        found_views, marks["views"] = view_sections(conn, explorer, store, base, now, to_params, covered)
        parts += found_views
    if enabled(store, "renewals"):
        marks["renewals"] = portfolio.due_reminders(conn, now)
        if marks["renewals"]:
            parts.append("\n".join(["Renouvellements :"] + [text for _, _, text in marks["renewals"]]))
    if (enabled(store, "weekly") and now_local.weekday() == weekly_day(store) and store.get("alert_weekly_last") != marks["day"]
            and conn.execute("SELECT 1 FROM domains LIMIT 1").fetchone()):
        parts.append(digest.build(checker, now))
        marks["weekly"] = True
    section, marks["health"] = morning.health_section(store)
    if section and enabled(store, "health"):
        parts.append(section)  # décoché : les événements sont tout de même effacés, rien ne s'accumule
    if not parts:
        return [], marks
    parts[0] = f"Résumé du {now_local.strftime('%d/%m')}\n\n" + parts[0]
    if base:
        parts.append(f"Aujourd'hui : {base}/")
    return pack(parts), marks


def pack(parts: list[str]) -> list[str]:
    """Rubriques regroupées en messages de 4 000 caractères au plus, sans couper une rubrique (sauf trop longue)."""
    messages, text = [], ""
    for part in parts:
        part = part if len(part) <= MAX_TEXT else part[:MAX_TEXT - 10] + "\n[tronqué]"
        joined = f"{text}\n\n{part}" if text else part
        if text and len(joined) > MAX_TEXT:
            messages.append(text)
            text = part
        else:
            text = joined
    return messages + ([text] if text else [])


def commit_daily(explorer, store, marks: dict, now: datetime) -> None:
    """Le quotidien est parti : chaque chose dite est notée, pour ne pas la redire demain."""
    conn = explorer.conn
    pepites.mark_daily(conn, marks.get("pepites") or [], now)
    mark_views(store, marks.get("views") or [], now)
    for item_id, key, _ in marks.get("renewals") or []:
        portfolio.mark_reminded(conn, item_id, key)
    if marks.get("weekly"):
        store.set("alert_weekly_last", marks["day"])
    morning.consume_health(store, marks.get("health") or 0)
    store.set("alert_daily_last", marks["day"])


# --- Essai ----------------------------------------------------------------------------------

def trial(explorer, store, now: datetime, base: str, order_url: str, version: str) -> str:
    """« Envoyer un essai » : les types activés et leur rythme, et l'exemple de la meilleure pépite du moment.
    Ne note rien : ni pépite annoncée, ni quotidien fait."""
    on = [(label, rhythm) for key, label, rhythm, _ in TYPES if enabled(store, key)]
    lines = [f"Essai des alertes domain-watch, version {version}"]
    immediate = [label.lower() for label, rhythm in on if rhythm == IMMEDIATE]
    later = [label.lower() for label, rhythm in on if rhythm != IMMEDIATE]
    lines.append("Immédiat : " + (", ".join(immediate) if immediate else "aucun type activé"))
    lines.append(f"Chaque jour à {daily_hour(store)} h : " + (", ".join(later) if later else "aucun type activé"))
    found = pepites.scan(explorer, now, pepite_min(store))
    best = pepites.order([p for p in found.values() if p["state"] in pepites.OPEN + (pepites.COMING,)])
    if best:
        lines += ["", "Exemple, la meilleure pépite du moment :"] + pepites.block(best[0], base, order_url, "Pépite")
    else:
        lines += ["", f"Aucune pépite en ce moment (avis « À prendre », revente {pepite_min(store)} ou plus, libre ou bientôt)."]
    if base:
        lines += ["", f"Aujourd'hui : {base}/"]
    return "\n".join(lines)
