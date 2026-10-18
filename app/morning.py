"""Messages Telegram sur les noms et sur la santé de l'appli.

- Résumé du matin : noms libérés et noms entrés en rédemption ou en suppression depuis 24 h,
  noms .co / .ai / .io sortis du DNS ; seulement les bons (score minimum ou avis IA favorable),
  chacun avec le lien direct vers sa fiche. Rien à signaler : pas de message.
- Alertes de panne : lecture de zone en erreur ou en retard, .fr en erreur ou en pause longue,
  notation IA de nuit muette, passe DNS en erreur ou à vide, exécutions manquées du planificateur.
  Un message à l'apparition du problème, un autre quand il disparaît.
"""

import json
from datetime import datetime, timedelta
from urllib.parse import urlencode

from . import ai, digest, drops, states
from .checker import iso, parse_iso

MAX_LINES = 10            # noms par rubrique
ZONE_LATE = timedelta(hours=36)
AI_LATE = timedelta(hours=36)
FR_PAUSE_ALERT = timedelta(hours=1)
SCHEDULER_MISSED_MAX = 3   # exécutions manquées en 24 h tolérées avant l'alerte
GOOD_VERDICTS = ("À prendre", "À surveiller")


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


def build(conn, explorer, cc_status: dict, base: str, now: datetime, min_score: int) -> str | None:
    since = iso(now - timedelta(hours=24))
    freed = [r["name"] for r in conn.execute(
        "SELECT name FROM drops WHERE state = ? AND available_since >= ?", (states.AVAILABLE, since))]
    coming = {r["name"]: r for r in conn.execute(
        f"SELECT name, state, release_at FROM drops WHERE state IN ({','.join('?' * len(drops.TRACKED))}) "
        "AND state_since >= ?", (*drops.TRACKED, since))}
    values = explorer.value_of(freed + list(coming))
    best_freed = sorted((n for n in freed if _good(values[n], min_score)), key=lambda n: -values[n]["score"])
    best_coming = sorted((n for n in coming if _good(values[n], min_score)), key=lambda n: -values[n]["score"])
    lost = []
    for tld, status in cc_status.items():
        finished = parse_iso(status.get("finished_at"))
        if finished and finished >= now - timedelta(hours=24):
            lost += status.get("freed_names") or []
    # Un nom sorti du DNS passe aussi dans la Veille : déjà listé plus haut avec son état réel, il n'est pas répété.
    shown = set(best_freed[:MAX_LINES]) | set(best_coming[:MAX_LINES])
    lost = [n for n in lost if n not in shown]
    if not best_freed and not best_coming and not lost:
        return None
    lines = ["Ce matin"]
    if best_freed:
        lines.append(f"\nLibérés en 24 h ({len(best_freed)} sur {len(freed)}) :")
        lines += [_line(n, values[n], base) for n in best_freed[:MAX_LINES]]
    if best_coming:
        lines.append(f"\nEntrés en rédemption ou en suppression ({len(best_coming)} sur {len(coming)}) :")
        for n in best_coming[:MAX_LINES]:
            release = parse_iso(coming[n]["release_at"])
            lines.append(_line(n, values[n], base, f"libre ~{release.astimezone(digest.local_zone()).strftime('%d/%m')}" if release else ""))
    if lost:
        lines.append(f"\nSortis du DNS (.co, .ai, .io), à vérifier : {', '.join(lost[:20])}"
                     + (f" et {len(lost) - 20} autres" if len(lost) > 20 else ""))
    if base:
        lines.append(f"\nVeille : {base}/")
    text = "\n".join(lines)
    return text if len(text) < 4000 else text[:3990] + "\n[tronqué]"


def due(store, now_local: datetime) -> bool:
    if store.get("morning_enabled", "1") != "1":
        return False
    hour = int(store.get("morning_hour", "8"))
    return now_local.hour >= hour and store.get("morning_last_sent") != now_local.date().isoformat()


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
    if state.explorer.fr.store.get("ai_token_hash") and last and now - last > AI_LATE:
        found["ai"] = f"Notation IA de nuit : aucun avis reçu depuis le {last.strftime('%d/%m %H:%M')} UTC"
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


def health_message(store, current: dict[str, str]) -> str | None:
    """Nouveaux problèmes et problèmes réglés depuis le dernier passage ; None s'il n'y a rien à dire."""
    before = json.loads(store.get("health_open") or "{}")
    opened = [text for key, text in current.items() if key not in before]
    closed = [text for key, text in before.items() if key not in current]
    store.set("health_open", json.dumps(current))
    if not opened and not closed:
        return None
    lines = []
    if opened:
        lines += ["Problème :"] + opened
    if closed:
        lines += (["", "Réglé :"] if opened else ["Réglé :"]) + closed
    return "\n".join(lines)
