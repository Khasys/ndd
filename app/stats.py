"""Page Statistiques : les chiffres qui disent si l'appli trouve les bons noms, et ce que fait le marché.

Tout se lit dans la base (journal des enregistrements, Veille, lectures de zones, avis IA, Gandi), en
quelques requêtes groupées ; le résultat est gardé CACHE en mémoire (la page se rouvre souvent).
"""

import json
import logging
import statistics
import threading
import time
from datetime import date, timedelta

from . import ai, escalation, wordlists
from .checker import parse_iso, utcnow

DAYS = 60          # graphes par jour
CATEGORY_DAYS = 30
RETAKE_DAYS = 120  # noms libérés regardés pour la reprise
RETAKE_MIN_AGE = 7  # une libération de moins d'une semaine n'a pas encore eu le temps d'être reprise
MIN_GROUP = 30     # part reprise affichée seulement sur au moins 30 noms
SCORE_BANDS = ((0, 19), (20, 39), (40, 59), (60, 79), (80, 100))
CACHE = 600
log = logging.getLogger(__name__)
_cache: dict = {}
_lock = threading.Lock()


def build(conn, tlds: list[str]) -> dict:
    with _lock:
        hit = _cache.get("stats")
        if hit and time.monotonic() - hit[0] < CACHE:
            return hit[1]
        today = utcnow().date()
        found = {"generated_at": utcnow().isoformat(timespec="seconds"), "tlds": tlds, "errors": []}
        blocks = {"registrations": lambda: registrations(conn, today), "releases": lambda: releases(conn, today),
                  "zones": lambda: zones(conn), "ai": lambda: ai_stats(conn, today), "gandi": lambda: gandi(conn, today)}
        for key, block in blocks.items():
            # Un bloc en erreur (donnée inattendue) : les autres s'affichent, la cause va au journal.
            try:
                found[key] = block()
            except Exception:
                log.exception("Statistiques : bloc %s en erreur", key)
                found[key] = None
                found["errors"].append(key)
        _cache["stats"] = (time.monotonic(), found)
        return found


def clear() -> None:
    _cache.clear()


def _days(today: date, n: int = DAYS) -> list[str]:
    return [(today - timedelta(days=n - 1 - k)).isoformat() for k in range(n)]


def _per_day(rows, days: list[str]) -> dict[str, list[int]]:
    """(jour, extension, nombre) -> {extension: [nombre par jour]}."""
    index = {d: k for k, d in enumerate(days)}
    out: dict[str, list[int]] = {}
    for day, tld, n in rows:
        if day in index:
            out.setdefault(tld, [0] * len(days))[index[day]] += n
    return out


def _band(score: int) -> str:
    return next(f"{low}-{high}" for low, high in SCORE_BANDS if low <= max(0, min(100, score)) <= high)


def registrations(conn, today: date) -> dict:
    days = _days(today)
    rows = conn.execute("SELECT day, tld, COUNT(*) FROM registrations WHERE day >= ? GROUP BY day, tld",
                        (days[0],)).fetchall()
    first = conn.execute("SELECT MIN(day), COUNT(*) FROM registrations").fetchone()
    since30 = (today - timedelta(days=CATEGORY_DAYS - 1)).isoformat()
    since7 = (today - timedelta(days=6)).isoformat()
    total7, total30 = (conn.execute("SELECT COUNT(*) FROM registrations WHERE day >= ?", (d,)).fetchone()[0]
                       for d in (since7, since30))
    categories: dict[str, int] = {}
    bands = {f"{low}-{high}": 0 for low, high in SCORE_BANDS}
    verdicts: dict[str, int] = {}
    for cats, score, verdict in conn.execute(
            "SELECT categories, score, ai_verdict FROM registrations WHERE day >= ? AND enriched_at IS NOT NULL", (since30,)):
        for c in (cats or "").split(","):
            if c:
                categories[c] = categories.get(c, 0) + 1
        if score is not None:
            bands[_band(score)] += 1
        if verdict:
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
    # Repris après une libération : délai entre la suppression et le nouvel enregistrement.
    delays: dict[str, list[int]] = {}
    for tld, day, removed in conn.execute(
            "SELECT tld, day, removed_on FROM registrations WHERE removed_on IS NOT NULL AND day >= ?", (since30,)):
        if not removed:
            continue  # sans sortie constatée
        gap = (date.fromisoformat(day) - date.fromisoformat(removed[:10])).days
        if gap >= 0:
            delays.setdefault(tld, []).append(gap)
    pending = conn.execute("SELECT COUNT(*) FROM registrations WHERE enriched_at IS NULL").fetchone()[0]
    return {
        "days": days, "by_day": _per_day(rows, days), "since": first[0], "total": first[1],
        "total_7": total7, "total_30": total30, "pending_context": pending,
        "categories": [{"key": k, "label": wordlists.LABELS.get(k, k), "count": n}
                       for k, n in sorted(categories.items(), key=lambda x: -x[1])[:12]],
        "score_bands": [{"band": b, "count": n} for b, n in bands.items()],
        "ai_verdicts": verdicts,
        "retaken": {t: {"count": len(v), "median_days": statistics.median(v)} for t, v in sorted(delays.items())},
    }


def releases(conn, today: date) -> dict:
    """Veille : libérations par jour, et part des noms libérés repris par quelqu'un (et en combien de temps)."""
    days = _days(today)
    rows = conn.execute("SELECT removed_on, tld, COUNT(*) FROM drops WHERE removed_on >= ? GROUP BY removed_on, tld",
                        (days[0],)).fetchall()
    start = (today - timedelta(days=RETAKE_DAYS)).isoformat()
    end = (today - timedelta(days=RETAKE_MIN_AGE)).isoformat()
    by_tld: dict[str, dict] = {}
    by_cat: dict[str, dict] = {}
    for tld, removed, state, since, lists in conn.execute(
            "SELECT tld, removed_on, state, state_since, lists FROM drops WHERE removed_on BETWEEN ? AND ?", (start, end)):
        taken = state == "taken"
        groups = [by_tld.setdefault(tld, {"count": 0, "taken": 0, "delays": []})]
        groups += [by_cat.setdefault(c, {"count": 0, "taken": 0, "delays": []}) for c in (lists or "").split(",") if c]
        delay = None
        if taken and since:
            stamp = parse_iso(since)
            delay = (stamp.date() - date.fromisoformat(removed)).days if stamp else None
        for g in groups:
            g["count"] += 1
            g["taken"] += taken
            if delay is not None and delay >= 0:
                g["delays"].append(delay)

    def shape(groups: dict, labels=None) -> list[dict]:
        out = []
        for key, g in groups.items():
            if g["count"] < MIN_GROUP:
                continue
            out.append({"key": key, "label": (labels or {}).get(key, key), "count": g["count"], "taken": g["taken"],
                        "share": round(g["taken"] / g["count"], 3),
                        "median_days": statistics.median(g["delays"]) if g["delays"] else None})
        return sorted(out, key=lambda x: -x["share"])

    return {"days": days, "by_day": _per_day(rows, days), "window": [start, end],
            "by_tld": shape(by_tld), "by_category": shape(by_cat, wordlists.LABELS)[:12]}


def zones(conn) -> list[dict]:
    """Dernière lecture réussie de chaque zone, et la taille une semaine avant."""
    out = []
    for (tld,) in conn.execute("SELECT DISTINCT tld FROM zone_scans").fetchall():
        scans = conn.execute("SELECT at, names, added, removed FROM zone_scans WHERE tld = ? AND error IS NULL "
                             "AND names IS NOT NULL ORDER BY at DESC LIMIT 30", (tld,)).fetchall()
        if not scans:
            continue
        last = scans[0]
        week = next((s for s in scans if parse_iso(s["at"]) and parse_iso(last["at"]) - parse_iso(s["at"]) >= timedelta(days=6)), None)
        out.append({"tld": tld, "at": last["at"], "names": last["names"], "added": last["added"], "removed": last["removed"],
                    "week_change": last["names"] - week["names"] if week else None})
    return sorted(out, key=lambda z: -z["names"])


def ai_stats(conn, today: date) -> dict:
    days = _days(today, 30)
    per_day = dict(conn.execute("SELECT substr(rated_at, 1, 10) d, COUNT(*) FROM domain_ai WHERE rated_at >= ? GROUP BY d",
                                (days[0],)).fetchall())
    verdicts = {v: conn.execute("SELECT COUNT(*) FROM domain_ai WHERE version >= ? AND comment LIKE ?",
                                (ai.VERSION, f"{v}.%")).fetchone()[0] for v in ai.VERDICTS}
    panels = [json.loads(r[0]) for r in conn.execute("SELECT panel FROM domain_ai WHERE panel IS NOT NULL")]
    takes = [p["take"] for p in panels]
    outcomes = ai.outcomes(conn)
    return {"days": days, "per_day": [per_day.get(d, 0) for d in days], "verdicts": verdicts,
            "escalation": escalation.counts(conn),
            "panel": {"count": len(panels), "takes": [takes.count(k) for k in range(escalation.PANEL_VOTES + 1)],
                      "kept": verdicts_of_panel(conn)},
            "outcomes": {"total": outcomes["total"], "min_age_days": outcomes["min_age_days"],
                         "by_verdict": outcomes["by_verdict"]}}


def verdicts_of_panel(conn) -> int:
    """Noms confirmés « À prendre » par le panel."""
    return conn.execute("SELECT COUNT(*) FROM domain_ai WHERE panel IS NOT NULL AND comment LIKE 'À prendre.%'").fetchone()[0]


def gandi(conn, today: date) -> dict:
    """Vérifications Gandi : noms vus par jour (date de leur dernière vérification) et leur réponse."""
    days = _days(today, 30)
    rows = conn.execute("SELECT substr(checked_at, 1, 10) d, kind, COUNT(*) FROM gandi_checks WHERE checked_at >= ? "
                        "GROUP BY d, kind", (days[0],)).fetchall()
    per_day = {d: 0 for d in days}
    kinds: dict[str, int] = {}
    for d, kind, n in rows:
        if d in per_day:
            per_day[d] += n
        kinds[kind] = kinds.get(kind, 0) + n
    return {"days": days, "per_day": [per_day[d] for d in days], "kinds": kinds}
