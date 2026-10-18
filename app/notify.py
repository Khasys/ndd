"""Alertes Telegram immédiates, à chaque tick, en plus du résumé du matin (morning.py) qui récapitule.

- « À prendre » : un nom de la Veille qui passe libre ou entre en rédemption, noté « À prendre » par l'IA
  avec une note de revente au moins égale au seuil réglé. Une alerte par nom et par état (table
  alert_sent) ; les noms d'un même tick partent dans un seul message.
- Vues enregistrées : chaque vue avec « alerte » reçoit les noms devenus libres ou bientôt libres depuis
  son dernier passage qui répondent à ses filtres, par le classement de l'Explorer lui-même.

Tout se lit dans la table drops : c'est là que chaque vérification (zones, .fr, DNS) pose l'état et sa
date, quel que soit le chemin pris. Rien à brancher dans drops.apply.
"""

import json
from datetime import datetime, timedelta

from . import states
from .checker import iso, parse_iso
from .morning import link

MAX_LINES = 10                 # noms détaillés par message, puis « et N autres »
WINDOW = timedelta(hours=48)   # passés libres ou en rédemption au-delà : trop vieux pour une alerte immédiate
VIEW_EVERY = timedelta(hours=1)
DEFAULT_MIN = 70
KIND_FREE, KIND_COMING = "free", "coming"
COMING_STATES = (states.REDEMPTION, states.PENDING_DELETE)
TAKE = "À prendre"


def enabled(store) -> bool:
    return store.get("alert_take_enabled", "1") == "1"


def min_resale(store) -> int:
    return int(store.get("alert_take_min") or DEFAULT_MIN)


def changed(conn, since: datetime) -> list[dict]:
    """Noms passés libres ou entrés en rédemption (ou en suppression) depuis `since`, les plus récents d'abord."""
    stamp = iso(since)
    rows = conn.execute(
        f"""SELECT name, state, state_since, available_since, release_at FROM drops
            WHERE (state = ? AND available_since >= ?)
               OR (state IN ({','.join('?' * len(COMING_STATES))}) AND state_since >= ?)
            ORDER BY COALESCE(available_since, state_since) DESC""",
        (states.AVAILABLE, stamp, *COMING_STATES, stamp),
    ).fetchall()
    return [{"name": r["name"], "kind": KIND_FREE if r["state"] == states.AVAILABLE else KIND_COMING,
             "release_at": r["release_at"]} for r in rows]


def _state_text(item: dict) -> str:
    if item["kind"] == KIND_FREE:
        return "libre"
    release = parse_iso(item.get("release_at"))
    if release:
        from .digest import local_zone  # import local : digest importe checker, qui importe alerts

        return f"libre ~{release.astimezone(local_zone()).strftime('%d/%m')}"
    return "bientôt libre"


def verdict_of(opinion: dict | None) -> str:
    return ((opinion or {}).get("comment") or "").split(".")[0]


def _price(low, high) -> str:
    return f"{low} à {high} €" if low != high else f"{high} €"


def take_message(conn, explorer, store, base: str, order_url: str, now: datetime) -> tuple[str | None, list[tuple[str, str]]]:
    """Message « À prendre » du tick et les (nom, état) qu'il couvre ; (None, []) s'il n'y a rien.
    L'appelant marque les noms envoyés (mark_sent) une fois le message parti."""
    if not enabled(store):
        return None, []
    threshold = min_resale(store)
    sent = {(r[0], r[1]) for r in conn.execute("SELECT name, kind FROM alert_sent")}
    hidden = {r[0] for r in conn.execute("SELECT name FROM drop_hidden")}
    items = [c for c in changed(conn, now - WINDOW) if (c["name"], c["kind"]) not in sent and c["name"] not in hidden]
    if not items:
        return None, []
    values = explorer.value_of([c["name"] for c in items])
    kept = []
    for c in items:
        opinion = values[c["name"]].get("ai")
        if opinion and verdict_of(opinion) == TAKE and opinion["resale"] >= threshold:
            kept.append((c, opinion))
    if not kept:
        return None, []
    kept.sort(key=lambda pair: -pair[1]["resale"])
    lines = [f"{TAKE} : {len(kept)} nom{'s' if len(kept) > 1 else ''}"]
    for c, opinion in kept[:MAX_LINES]:
        name = c["name"]
        reason = opinion["comment"].partition(". ")[2]
        lines += ["", f"{name} : {_state_text(c)}",
                  f"{TAKE}. Exploitation {opinion['exploit']}, revente {opinion['resale']}. "
                  f"Prix utilisateur final {_price(opinion['final_low'], opinion['final_high'])}."]
        if reason:
            lines.append(reason)
        url = link(base, name)
        if url:
            lines.append(url)
        if order_url:
            lines.append("Réserver : " + order_url.replace("{domain}", name))
    if len(kept) > MAX_LINES:
        lines += ["", f"et {len(kept) - MAX_LINES} autres"]
    return "\n".join(lines), [(c["name"], c["kind"]) for c, _ in kept]


def mark_sent(conn, pairs: list[tuple[str, str]], now: datetime) -> None:
    conn.executemany("INSERT OR IGNORE INTO alert_sent (name, kind, sent_at) VALUES (?, ?, ?)",
                     [(name, kind, iso(now)) for name, kind in pairs])


# --- Vues enregistrées ---------------------------------------------------------------

def views_due(store, now: datetime) -> bool:
    """Une passe par heure au plus ; sans vue en alerte, rien à faire."""
    if not any(v.get("alert") for v in json.loads(store.get("explore_views") or "[]")):
        return False
    last = parse_iso(store.get("view_alert_run"))
    return last is None or now - last >= VIEW_EVERY


def matches(explorer, params: dict, name: str) -> bool:
    """Le nom répond aux filtres de la vue : même classement que l'Explorer, sur ce seul nom."""
    label, _, tld = name.partition(".")
    if tld not in params["tlds"]:
        return False
    rest = {k: v for k, v in params.items() if k not in ("tld", "tlds", "cursor", "q", "mode")}
    found = explorer.rank(tld, q=label, mode="exact", limit=1, **rest)["items"]
    return bool(found) and found[0]["name"] == name


def view_messages(conn, explorer, store, base: str, now: datetime, to_params) -> list[dict]:
    """Un message par vue en alerte : {"view": nom, "text": message ou None}. `to_params(filters)`
    traduit les filtres enregistrés comme ceux de l'adresse de l'Explorer (None : filtres invalides).
    L'appelant note le passage (mark_view) une fois le message parti, ou tout de suite s'il n'y a rien."""
    store.set("view_alert_run", iso(now))
    last = json.loads(store.get("view_alert_last") or "{}")
    out = []
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
        since = parse_iso(last.get(view["name"])) or now - timedelta(hours=24)
        found = [c for c in changed(conn, since) if matches(explorer, params, c["name"])]
        text = None
        if found:
            lines = [f"Vue « {view['name']} » : {len(found)} nouveau{'x' if len(found) > 1 else ''} nom{'s' if len(found) > 1 else ''}"]
            for c in found[:MAX_LINES]:
                lines.append(f"{c['name']} ({_state_text(c)})")
                url = link(base, c["name"])
                if url:
                    lines.append(url)
            if len(found) > MAX_LINES:
                lines.append(f"et {len(found) - MAX_LINES} autres")
            text = "\n".join(lines)
        out.append({"view": view["name"], "text": text})
    return out


def mark_view(store, view_name: str, now: datetime) -> None:
    last = json.loads(store.get("view_alert_last") or "{}")
    last[view_name] = iso(now)
    store.set("view_alert_last", json.dumps(last))
