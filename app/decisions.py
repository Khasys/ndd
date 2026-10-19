"""Décisions gardées sur un nom : ignoré (Veille) ou acheté (portefeuille).

Un nom ignoré puis enregistré par quelqu'un d'autre est un regret : la liste le dit, avec la date
quand elle est connue. Rien n'est effacé tant que la décision tient.
"""

from datetime import datetime

from . import drops, explore
from .checker import iso

KINDS = ("ignored", "bought")
TAKEN_NOW = (explore.REGISTERED, explore.SUSPENDED, explore.TAKEN_GANDI)
NOTE_MAX = 200


def set_decision(conn, name: str, decision: str, note: str | None, at: datetime) -> None:
    conn.execute(
        """INSERT INTO decisions (name, decision, at, note) VALUES (?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET decision = excluded.decision, at = excluded.at, note = excluded.note""",
        (name, decision, iso(at), note),
    )


def set_many(conn, names: list[str], decision: str, at: datetime) -> None:
    conn.executemany(
        """INSERT INTO decisions (name, decision, at, note) VALUES (?, ?, ?, NULL)
           ON CONFLICT(name) DO UPDATE SET decision = excluded.decision, at = excluded.at""",
        [(n, decision, iso(at)) for n in names],
    )


def delete(conn, name: str) -> bool:
    return bool(conn.execute("DELETE FROM decisions WHERE name = ?", (name,)).rowcount)


def forget_ignored(conn, names: list[str] | None = None) -> None:
    """Masquage annulé : la décision « ignoré » part avec lui (tous les noms masqués, ou ceux donnés)."""
    if names is None:
        conn.execute("DELETE FROM decisions WHERE decision = 'ignored' AND name IN (SELECT name FROM drop_hidden)")
    else:
        conn.executemany("DELETE FROM decisions WHERE decision = 'ignored' AND name = ?", [(n,) for n in names])


def listing(conn, explorer) -> dict:
    """Toutes les décisions, avec l'état d'aujourd'hui et, pour un nom ignoré repris, la date si connue."""
    rows = conn.execute("SELECT * FROM decisions ORDER BY at DESC, name").fetchall()
    mine = {r[0] for r in conn.execute("SELECT name FROM domains")} | {r[0] for r in conn.execute("SELECT name FROM portfolio")}
    u = explorer.universe
    data = {t: explorer.status_bytes(t) for t in explorer.tlds()}
    taken_dates = dict(conn.execute("SELECT name, state_since FROM drops WHERE state = ?", (drops.TAKEN,)).fetchall())
    taken_dates.update({k: v for k, v in conn.execute("SELECT name, taken_at FROM ai_outcomes WHERE taken_at IS NOT NULL") if k not in taken_dates})
    items, regrets = [], 0
    for r in rows:
        label, _, tld = r["name"].partition(".")
        i = u.index(label)
        code = int(data[tld][i]) if tld in data and i is not None else None
        taken = code in TAKEN_NOW and r["name"] not in mine
        outcome = "repris" if r["decision"] == "ignored" and taken else None
        regrets += outcome is not None
        items.append({"name": r["name"], "decision": r["decision"], "at": r["at"], "note": r["note"],
                      "state": code, "state_label": (explore.absent_label(tld) if code == explore.ABSENT else explore.LABELS.get(code))
                      if code is not None else None,
                      "outcome": outcome, "taken_at": taken_dates.get(r["name"]) if outcome else None})
    return {"items": items, "regrets": regrets}
