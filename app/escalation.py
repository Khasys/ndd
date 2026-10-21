"""Escalade des avis IA : un seul avis se trompe souvent, dans un sens comme dans l'autre.

- Repêchage : un « À surveiller » qui rapporte au moins RESCUE_YIELD euros par an reçoit RESCUE_VOTES contre-avis
  à l'aveugle ; si l'un dit « À prendre », il passe au panel.
- Panel : tout « À prendre » (premier avis ou repêché) est noté à l'aveugle par PANEL_VOTES correcteurs. Il reste
  « À prendre » si au moins PANEL_TAKE d'entre eux le disent et si la grille médiane, critère par critère, le dit
  aussi ; une faute d'orthographe ou un risque de marque relevé par VETO correcteurs le bloque. La médiane des
  dix avis remplace le premier avis, qui ne compte pas dans la décision.

La tâche de nuit (après la notation de nuit) reçoit une étape à la fois : le repêchage, puis le panel, qui reprend
les repêchés de la même nuit. Les noms sont réservés CLAIM (un passage interrompu les rend ensuite) ; chaque
correcteur est un agent séparé qui note tout le lot d'un coup. Un nom ne
repasse jamais une étape déjà faite ; seule une nouvelle analyse demandée depuis la fiche le remet en jeu.
"""

import json
import math
import secrets
import statistics
from datetime import timedelta

from .ai_format import CRITERIA_V5, check_any, derive, fit_prices, is_v5, normalized
from .checker import iso, parse_iso, utcnow

RESCUE_VOTES = 2
PANEL_VOTES = 10
VOTES = {"rescue": RESCUE_VOTES, "panel": PANEL_VOTES}
RESCUE_YIELD = 5.0   # euros par an : seuil de départ, à régler sur les noms repêchés qui finissent « À prendre »
PANEL_TAKE = 7       # « À prendre » d'au moins 7 correcteurs sur 10
VETO = 3             # faute ou risque de marque relevé par 3 correcteurs : pas de « À prendre »
VETO_RISK = 5        # risque de 5 et plus : marque connue dans un secteur (consigne)
CLAIM = timedelta(minutes=90)  # un passage de nuit sur un gros lot peut durer
# Par passage (une fois par nuit, après la notation de nuit) : chaque agent note tout le lot d'un coup ; son
# coût tient surtout à son lancement, pas au nombre de noms.
MAX_PANEL = 80
MAX_RESCUE = 200
PRICES = ("quick_low", "quick_high", "final_low", "final_high")


def consider(conn, name: str, verdict: str, gain) -> str | None:
    """Après un premier avis : le nom entre à l'étape qui lui revient, s'il n'est jamais passé par l'escalade."""
    if verdict == "À prendre":
        stage = "panel"
    elif verdict == "À surveiller" and (gain or 0) >= RESCUE_YIELD:
        stage = "rescue"
    else:
        return None
    cur = conn.execute("INSERT OR IGNORE INTO ai_escalation (name, stage, queued_at) VALUES (?, ?, ?)",
                       (name, stage, iso(utcnow())))
    return stage if cur.rowcount else None


def reset(conn, name: str) -> None:
    """Nouvelle analyse demandée depuis la fiche : l'escalade repart de zéro sur le nouvel avis."""
    conn.execute("DELETE FROM ai_escalation WHERE name = ?", (name,))
    conn.execute("DELETE FROM ai_votes WHERE name = ?", (name,))


def mark_notify(conn, names) -> None:
    """Analyse demandée qui part en escalade : le message Telegram attend le verdict définitif."""
    for name in names:
        conn.execute("UPDATE ai_escalation SET notify = 1 WHERE name = ? AND stage != 'done'", (name,))


def state(conn, name: str) -> dict | None:
    row = conn.execute("SELECT stage, queued_at FROM ai_escalation WHERE name = ? AND stage != 'done'", (name,)).fetchone()
    return {"stage": row["stage"], "queued_at": row["queued_at"]} if row else None


def counts(conn) -> dict:
    found = {"rescue": 0, "panel": 0}
    for stage, n in conn.execute("SELECT stage, COUNT(*) FROM ai_escalation WHERE stage != 'done' GROUP BY stage"):
        found[stage] = n
    return found


def claim(conn, describe, stage: str | None = None) -> dict:
    """Noms à noter pour ce passage, réservés CLAIM ; `describe(names)` les décrit comme ceux de la file, sans
    aucun avis déjà donné (à l'aveugle)."""
    now = utcnow()
    free = "(claimed_until IS NULL OR claimed_until < ?)"
    picked = {}
    for step, limit in (("panel", MAX_PANEL), ("rescue", MAX_RESCUE)):
        if stage and step != stage:
            # Une étape à la fois : le repêchage d'abord, puis le panel, qui reprend les repêchés de la même nuit.
            picked[step] = []
            continue
        picked[step] = [r[0] for r in conn.execute(
            f"SELECT name FROM ai_escalation WHERE stage = ? AND {free} ORDER BY queued_at, name LIMIT ?",
            (step, iso(now), limit))]
    token = secrets.token_hex(8) if any(picked.values()) else ""
    for names in picked.values():
        for name in names:
            conn.execute("UPDATE ai_escalation SET claimed_until = ?, claim = ? WHERE name = ?", (iso(now + CLAIM), token, name))
    return {"claim": token, "votes": VOTES, **{step: describe(names) if names else [] for step, names in picked.items()}}


def record(conn, stage: str, claim_id: str, voter: int, results: list, context_of) -> dict:
    """Avis d'un correcteur pour une étape ; à l'étape complète, la décision. Renvoie aussi les noms dont le
    verdict est devenu définitif ({"name", "notify"}), pour les messages."""
    if stage not in VOTES:
        return {"saved": 0, "rejected": [{"name": "", "reason": "étape inconnue"}], "final": []}
    waiting = {r[0] for r in conn.execute("SELECT name FROM ai_escalation WHERE stage = ?", (stage,))}
    now = iso(utcnow())
    saved, rejected, touched = 0, [], []
    for item in results:
        name = item.get("name") if isinstance(item, dict) else None
        if is_v5(item):
            item = fit_prices(item)  # un prix hors palier est ramené, pas refusé : le correcteur compte quand même
        reason = check_any(item, lambda n: n in waiting)
        if not reason and not is_v5(item):
            reason = "grille détaillée attendue"
        if reason:
            rejected.append({"name": str(name or ""), "reason": "pas à cette étape" if reason == "nom inconnu" else reason})
            continue
        item = normalized({k: v for k, v in item.items() if k not in ("quick_months", "final_months")})
        notes = derive({**item, **context_of(name)})
        conn.execute("INSERT OR IGNORE INTO ai_votes (name, stage, claim, voter, item, verdict, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (name, stage, claim_id, voter, json.dumps(item, ensure_ascii=False), notes["verdict"], now))
        saved += 1
        touched.append(name)
    final = []
    for name in dict.fromkeys(touched):
        votes = conn.execute("SELECT item, verdict FROM ai_votes WHERE name = ? AND stage = ? ORDER BY at, claim, voter",
                             (name, stage)).fetchall()
        if len(votes) < VOTES[stage]:
            continue
        votes = votes[:VOTES[stage]]
        if stage == "rescue":
            if any(v["verdict"] == "À prendre" for v in votes):
                conn.execute("UPDATE ai_escalation SET stage = 'panel', claimed_until = NULL, claim = NULL WHERE name = ?", (name,))
                continue
            result = {"rescue": [v["verdict"] for v in votes]}
        else:
            result = decide(conn, name, [json.loads(v["item"]) for v in votes], [v["verdict"] for v in votes],
                            context_of(name))
        row = conn.execute("SELECT notify FROM ai_escalation WHERE name = ?", (name,)).fetchone()
        conn.execute("UPDATE ai_escalation SET stage = 'done', done_at = ?, result = ?, claimed_until = NULL WHERE name = ?",
                     (now, json.dumps(result, ensure_ascii=False), name))
        final.append({"name": name, "notify": bool(row and row["notify"])})
    return {"saved": saved, "rejected": rejected, "final": final}


def _median(values) -> int:
    return int(math.floor(statistics.median(values) + 0.5))


def decide(conn, name: str, items: list[dict], verdicts: list[str], context: dict) -> dict:
    """Décision du panel, écrite à la place de l'avis : grille et prix médians, verdict à la majorité qualifiée."""
    grid = {k: _median([i[k] for i in items]) for k in CRITERIA_V5}
    prices = {k: _median([i[k] for i in items]) for k in PRICES}
    prices["quick_low"], prices["quick_high"] = sorted((prices["quick_low"], prices["quick_high"]))
    prices["final_low"], prices["final_high"] = sorted((prices["final_low"], prices["final_high"]))
    notes = derive({"name": name, **grid, **prices, **context})
    takes = verdicts.count("À prendre")
    faults = sum(1 for i in items if i["comment"].startswith("Faute"))
    risks = sum(1 for i in items if i["risque"] >= VETO_RISK)
    veto = "faute" if faults >= VETO else "marque" if risks >= VETO else ""
    if notes["verdict"] == "À laisser":
        verdict = "À laisser"
    elif notes["verdict"] == "À prendre" and takes >= PANEL_TAKE and not veto:
        verdict = "À prendre"
    else:
        verdict = "À surveiller"
    # La raison : celle du correcteur d'accord avec la décision dont la note de revente est la plus proche de la médiane.
    resales = [derive({"name": name, **i})["resale"] for i in items]
    middle = statistics.median(resales)
    agree = [n for n, v in enumerate(verdicts) if v == verdict] or list(range(len(items)))
    chosen = min(agree, key=lambda n: (abs(resales[n] - middle), n))
    panel = {"take": takes, "watch": verdicts.count("À surveiller"), "skip": verdicts.count("À laisser"), "n": len(items),
             "veto": veto, "median_verdict": notes["verdict"], "resale_min": min(resales), "resale_max": max(resales),
             "at": iso(utcnow())}
    row = {"exploit": notes["exploit"], "resale": notes["resale"], **prices, "comment": f"{verdict}. {items[chosen]['comment']}",
           "grid": json.dumps(grid), "sell_rate": notes.get("sell_rate"), "yield": notes.get("yield_"),
           "taken": context.get("taken"), "renewal": context.get("renewal"), "panel": json.dumps(panel),
           "confirmed_at": panel["at"]}
    cur = conn.execute(f"UPDATE domain_ai SET {', '.join(f'{k} = :{k}' for k in row)}, "
                       "reviewed_at = COALESCE(reviewed_at, :confirmed_at) WHERE name = :name", {**row, "name": name})
    if not cur.rowcount:  # avis effacé entre-temps : la décision le recrée
        from .ai import VERSION
        row.update(name=name, rated_at=panel["at"], version=VERSION)
        conn.execute(f"INSERT INTO domain_ai ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})", row)
    return {"verdict": verdict, **panel}


def stale_claims(conn) -> int:
    """Réservations échues (passage interrompu) : le nom revient dans la file au passage suivant (pour /api/diag)."""
    now = utcnow()
    return sum(1 for (until,) in conn.execute("SELECT claimed_until FROM ai_escalation WHERE stage != 'done' AND claimed_until IS NOT NULL")
               if parse_iso(until) < now)
