"""Contenu du quotidien (notify.py) : récapitulatif des bons noms et santé de l'appli.

- Récapitulatif : noms libérés et noms entrés en rédemption ou en suppression depuis 24 h, libres selon le
  WHOIS, noms .co / .ai / .io sortis du DNS ; seulement les bons (score minimum ou avis IA favorable), chacun
  avec le lien direct vers sa fiche, sans les pépites (annoncées à part). Rien à signaler : pas de rubrique.
- Santé : lecture de zone en erreur ou en retard, .fr en erreur ou en pause longue, notation IA de nuit muette,
  passe DNS en erreur ou à vide, Gandi en erreur, exécutions manquées du planificateur. Le contrôle de santé (toutes les 10 minutes) note
  l'apparition et la disparition de chaque problème (health_events) ; le quotidien les dit une fois, puis rappelle
  d'une ligne ceux qui durent. Les pannes bloquantes (BLOCKING) partent tout de suite (notify.urgent_tick), et le
  quotidien ne les redit qu'en « Toujours en cours ».
"""

import json
from datetime import datetime, timedelta
from urllib.parse import urlencode

from . import ai, digest, drops, states, tokens
from .checker import iso, parse_iso

MAX_LINES = 10            # noms par rubrique
ZONE_LATE = timedelta(hours=36)
AI_LATE = timedelta(hours=36)
FR_PAUSE_ALERT = timedelta(hours=1)
SCHEDULER_MISSED_MAX = 3   # exécutions manquées en 24 h tolérées avant l'alerte
GOOD_VERDICTS = ("À prendre", "À surveiller")
MAX_EVENTS = 50          # événements de santé gardés pour le quotidien
# Pannes qui arrêtent l'appli sur une partie de son travail : dites tout de suite, apparition et fin. Jeton Gandi
# refusé (passes de nuit et continue arrêtées), vérifications .fr en pause longue, zones non lues depuis 36 h,
# fichier .fr de l'Afnic en erreur. Le reste attend le quotidien.
BLOCKING = ("gandi", "fr_pause", "czds_late", "fr", "market")


def link(base: str, name: str) -> str:
    return f"{base}/?{urlencode({'fiche': name})}" if base else ""


def _good(value: dict, min_score: int) -> bool:
    opinion = value.get("ai") or {}
    return value.get("score", 0) >= min_score or (opinion.get("comment") or "").startswith(GOOD_VERDICTS)


def _line(name: str, value: dict, base: str, extra: str = "") -> str:
    opinion = (value.get("ai") or {}).get("comment", "")
    verdict = opinion.split(".")[0] if opinion else ""
    head = f"{name} ({value.get('score', 0)}" + (f", {verdict}" if verdict else "") + ")" + (f" {extra}" if extra else "")
    url = link(base, name)
    return f"{head}\n{url}" if url else head


def sections(conn, explorer, cc_status: dict, base: str, now: datetime, min_score: int,
             exclude: frozenset | set = frozenset()) -> list[str]:
    """Rubriques du récapitulatif des 24 dernières heures ; exclude : noms déjà annoncés ailleurs (pépites)."""
    since = iso(now - timedelta(hours=24))
    freed = [r["name"] for r in conn.execute(
        "SELECT name FROM drops WHERE state = ? AND available_since >= ? AND origin IS NOT 'gandi'", (states.AVAILABLE, since))
        if r["name"] not in exclude]
    coming = {r["name"]: r for r in conn.execute(
        f"SELECT name, state, release_at FROM drops WHERE state IN ({','.join('?' * len(drops.TRACKED))}) "
        "AND state_since >= ?", (*drops.TRACKED, since)) if r["name"] not in exclude}
    # Libres selon le seul WHOIS : pas des « libérés » ; listés à part, à confirmer chez un registrar.
    unsure = [r["name"] for r in conn.execute(
        "SELECT name FROM drops WHERE state = ? AND state_since >= ? AND origin IS NOT 'gandi' "
        "AND name NOT IN (SELECT name FROM drop_hidden)",
        (drops.UNCONFIRMED, since)) if r["name"] not in exclude]
    held = getattr(explorer, "reserved", None)
    if held is not None:  # marqué « Réservé par registre » : plus à prendre
        unsure = [n for n in unsure if n not in held.names()]
    values = explorer.value_of(freed + list(coming) + unsure)
    best_freed = sorted((n for n in freed if _good(values[n], min_score)), key=lambda n: -values[n]["score"])
    best_unsure = sorted((n for n in unsure if _good(values[n], min_score)), key=lambda n: -values[n]["score"])
    best_coming = sorted((n for n in coming if _good(values[n], min_score)), key=lambda n: -values[n]["score"])
    lost = []
    for tld, status in cc_status.items():
        finished = parse_iso(status.get("finished_at"))
        if finished and finished >= now - timedelta(hours=24):
            lost += status.get("freed_names") or []
    # Un nom sorti du DNS passe aussi dans la Veille : déjà listé plus haut avec son état réel, il n'est pas répété.
    shown = set(best_freed[:MAX_LINES]) | set(best_coming[:MAX_LINES]) | set(best_unsure[:MAX_LINES]) | set(exclude)
    lost = [n for n in lost if n not in shown]
    found = []
    if best_freed:
        found.append("\n".join([f"Libérés en 24 h ({len(best_freed)} sur {len(freed)}) :"]
                               + [_line(n, values[n], base) for n in best_freed[:MAX_LINES]]))
    if best_coming:
        lines = [f"Entrés en rédemption ou en suppression ({len(best_coming)} sur {len(coming)}) :"]
        for n in best_coming[:MAX_LINES]:
            release = parse_iso(coming[n]["release_at"])
            lines.append(_line(n, values[n], base, f"libre ~{release.astimezone(digest.local_zone()).strftime('%d/%m')}" if release else ""))
        found.append("\n".join(lines))
    if best_unsure:
        found.append("\n".join([f"Libres selon le WHOIS, à confirmer chez un registrar ({len(best_unsure)} sur {len(unsure)}) :"]
                               + [_line(n, values[n], base) for n in best_unsure[:MAX_LINES]]))
    if lost:
        found.append(f"Sortis du DNS, à vérifier : {', '.join(lost[:20])}"
                     + (f" et {len(lost) - 20} autres" if len(lost) > 20 else ""))
    return found


def build(conn, explorer, cc_status: dict, base: str, now: datetime, min_score: int,
          exclude: frozenset | set = frozenset()) -> str | None:
    """Récapitulatif seul, en un message (essais et tests) ; None s'il n'y a rien de bon."""
    found = sections(conn, explorer, cc_status, base, now, min_score, exclude)
    if not found:
        return None
    text = "\n\n".join(["Ce matin"] + found + ([f"Veille : {base}/"] if base else []))
    return text if len(text) < 4000 else text[:3990] + "\n[tronqué]"


# --- Santé ------------------------------------------------------------------------------

def problems(state, now: datetime) -> dict[str, str]:
    found = {}
    watcher = state.drops
    if watcher.store.get("czds_error"):
        found["czds"] = f"Zones ICANN : {watcher.store.get('czds_error')}"
    latest_ok = None
    for tld in watcher.enabled():
        status = watcher.status(tld)
        ok_at = parse_iso(status.get("ok_at"))
        latest_ok = max(latest_ok or ok_at, ok_at) if ok_at else latest_ok
        if status.get("error"):
            found[f"zone:{tld}"] = f"Lecture de la zone .{tld} en erreur : {status['error']}"
        elif ok_at and now - ok_at > ZONE_LATE:
            found[f"zone:{tld}"] = f"Zone .{tld} pas lue depuis le {ok_at.strftime('%d/%m %H:%M')} UTC"
    # Compte configuré, lectures déjà lancées, et aucune zone lue avec succès depuis 36 h (ou jamais).
    if watcher.configured and watcher.enabled() and watcher.last_run() and (latest_ok is None or now - latest_ok > ZONE_LATE):
        found["czds_late"] = (f"Zones ICANN : aucune lecture réussie depuis le {latest_ok.strftime('%d/%m %H:%M')} UTC"
                              if latest_ok else "Zones ICANN : aucune lecture réussie à ce jour")
    _dns_problems(found, state)
    missed = [e for e in (getattr(state, "scheduler_events", None) or {}).get("missed", ())
              if (parse_iso(e.get("at")) or now) >= now - timedelta(hours=24)]
    if len(missed) > SCHEDULER_MISSED_MAX:
        found["scheduler"] = f"Planificateur : {len(missed)} exécutions manquées en 24 h (dernière : {missed[-1].get('job')})"
    fr = state.fr
    if fr.enabled:
        error = json.loads(fr.store.get("fr_status") or "{}").get("error")
        if error:
            found["fr"] = f"Fichier .fr de l'Afnic : {error}"
        paused = fr.paused_until(now)
        if paused and paused - now > FR_PAUSE_ALERT:
            found["fr_pause"] = f"Vérifications .fr en pause jusqu'au {paused.strftime('%d/%m %H:%M')} UTC (limite Afnic)"
    last = parse_iso(ai.last_rated(state.explorer.conn))
    if tokens.any_of(state.explorer.conn, "ai") and last and now - last > AI_LATE:
        found["ai"] = f"Notation IA de nuit : aucun avis reçu depuis le {last.strftime('%d/%m %H:%M')} UTC"
    store = watcher.store
    if (store.get("gandi_pat") or "").strip() and store.get("gandi_error"):
        found["gandi"] = f"Gandi : {store.get('gandi_error')}"
    market = getattr(state, "market", None)
    if market is not None and (problem := market.problem(now)):
        found["market"] = problem  # source des ventes comparables cassée : alerte Telegram tout de suite
    return found


def _dns_problems(found: dict[str, str], state) -> None:
    """Passes DNS (.fr, .co, .ai, .io) : dernière passe en erreur, ou aucun nom vérifié alors qu'il y en avait en file."""
    passes = {t: c for t, c in (getattr(state, "cc_dns", None) or {}).items()}
    if getattr(state, "fr_dns", None) is not None:
        passes["fr"] = state.fr_dns
    for tld, checker in passes.items():
        status = checker.status()
        if not status.get("enabled", True) or not status.get("finished_at"):
            continue
        if status.get("error"):
            found[f"dns:{tld}"] = f"Passe DNS .{tld} en erreur : {status['error']}"
        elif status.get("queued") and not status.get("checked"):
            why = f" ({status['stopped']})" if status.get("stopped") else ""
            found[f"dns:{tld}"] = f"Passe DNS .{tld} : aucun nom vérifié sur {status['queued']} en file{why}"


def _events(store) -> list[dict]:
    try:
        return json.loads(store.get("health_events") or "[]")
    except ValueError:
        return []


def record_health(store, current: dict[str, str], now: datetime) -> int:
    """Contrôle de santé : note les problèmes apparus et ceux réglés depuis le passage précédent (health_events),
    pour le quotidien. Rien n'est envoyé ici. Renvoie le nombre d'événements notés."""
    before = json.loads(store.get("health_open") or "{}")
    events = _events(store)
    added = [{"at": iso(now), "kind": "open", "text": text, "urgent": key in BLOCKING}
             for key, text in current.items() if key not in before]
    added += [{"at": iso(now), "kind": "closed", "text": text, "urgent": key in BLOCKING}
              for key, text in before.items() if key not in current]
    store.set("health_open", json.dumps(current))
    if added:
        store.set("health_events", json.dumps((events + added)[-MAX_EVENTS:]))
    return len(added)


def note(store, text: str, now: datetime) -> None:
    """Événement ponctuel (passe DNS ou Gandi arrêtée) : dit une fois dans le quotidien."""
    store.set("health_events", json.dumps((_events(store) + [{"at": iso(now), "kind": "note", "text": text}])[-MAX_EVENTS:]))


EVENT_WORDS = {"open": "Apparu", "closed": "Réglé", "note": "Signalé"}


def _event_id(e: dict) -> tuple:
    return e.get("at"), e.get("kind"), e.get("text")


def urgent_events(store) -> list[dict]:
    """Pannes bloquantes apparues ou réglées, pas encore envoyées."""
    return [e for e in _events(store) if e.get("urgent") and not e.get("sent")]


def mark_urgent_sent(store, events: list[dict]) -> None:
    """Envoyées : le quotidien ne les redit pas (une panne qui dure y reste en « Toujours en cours »)."""
    ids = {_event_id(e) for e in events}
    store.set("health_events", json.dumps([{**e, "sent": True} if _event_id(e) in ids else e for e in _events(store)]))


def urgent_text(events: list[dict]) -> str:
    zone = digest.local_zone()
    lines = []
    for e in events:
        at = parse_iso(e.get("at"))
        when = f" ({at.astimezone(zone).strftime('%d/%m à %H:%M')})" if at else ""
        word = "Réglé" if e.get("kind") == "closed" else "Panne bloquante"
        lines.append(f"{word}{when} : {e.get('text', '')}")
    return "\n".join(lines)


def health_section(store) -> tuple[str | None, int]:
    """Rubrique Santé du quotidien et nombre d'événements qu'elle couvre (effacés une fois le message parti) :
    chaque événement une fois (sauf une panne bloquante déjà envoyée), puis une ligne par problème encore ouvert
    déjà signalé."""
    events = _events(store)
    zone = digest.local_zone()
    lines = []
    for e in events:
        if e.get("sent"):
            continue
        at = parse_iso(e.get("at"))
        when = f" le {at.astimezone(zone).strftime('%d/%m à %H:%M')}" if at else ""
        lines.append(f"{EVENT_WORDS.get(e.get('kind'), 'Signalé')}{when} : {e.get('text', '')}")
    told = {e.get("text") for e in events if e.get("kind") == "open" and not e.get("sent")}
    lines += [f"Toujours en cours : {text}" for text in json.loads(store.get("health_open") or "{}").values() if text not in told]
    if not lines:
        return None, len(events)
    return "\n".join(["Santé :"] + lines), len(events)


def consume_health(store, count: int) -> None:
    """Efface les événements dits dans le quotidien ; ceux notés depuis restent pour le suivant."""
    if count:
        store.set("health_events", json.dumps(_events(store)[count:]))
