"""Messages Telegram sur les noms et sur la santé de l'appli.

- Résumé du matin : noms libérés et noms entrés en rédemption ou en suppression depuis 24 h,
  noms .co / .ai / .io sortis du DNS ; seulement les bons (score minimum ou avis IA favorable),
  chacun avec le lien direct vers sa fiche. Rien à signaler : pas de message.
- Alertes de panne : lecture de zone en erreur ou en retard, .fr en erreur ou en pause longue,
  notation IA de nuit muette. Un message à l'apparition du problème, un autre quand il disparaît.
"""

import json
from datetime import datetime, timedelta
from urllib.parse import urlencode

from . import ai, drops, states
from .checker import iso, parse_iso

MAX_LINES = 10            # noms par rubrique
ZONE_LATE = timedelta(hours=36)
AI_LATE = timedelta(hours=36)
FR_PAUSE_ALERT = timedelta(hours=1)
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
            lines.append(_line(n, values[n], base, f"libre ~{release.strftime('%d/%m')}" if release else ""))
    if lost:
        lines.append(f"\nSortis du DNS (.co, .ai, .io), à vérifier : {', '.join(lost[:20])}"
                     + (f" et {len(lost) - 20} autres" if len(lost) > 20 else ""))
    if base:
        lines.append(f"\nSuppressions : {base}/")
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
    for tld in watcher.enabled():
        status = watcher.status(tld)
        ok_at = parse_iso(status.get("ok_at"))
        if status.get("error"):
            found[f"zone:{tld}"] = f"Lecture de la zone .{tld} en erreur : {status['error']}"
        elif ok_at and now - ok_at > ZONE_LATE:
            found[f"zone:{tld}"] = f"Zone .{tld} pas lue depuis le {ok_at.strftime('%d/%m %H:%M')} UTC"
    fr = state.fr
    if fr.enabled:
        error = json.loads(fr.store.get("fr_status") or "{}").get("error")
        if error:
            found["fr"] = f"Fichier .fr de l'Afnic : {error}"
        paused = fr.paused_until(now)
        if paused and paused - now > FR_PAUSE_ALERT:
            found["fr_pause"] = f"Vérifications .fr en pause jusqu'au {paused.strftime('%d/%m %H:%M')} UTC (limite Afnic)"
    stats = ai.stats(state.explorer)
    last = parse_iso(stats.get("last"))
    if stats.get("token_set") and last and now - last > AI_LATE:
        found["ai"] = f"Notation IA de nuit : aucun avis reçu depuis le {last.strftime('%d/%m %H:%M')} UTC"
    return found


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
