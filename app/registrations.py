"""Journal des enregistrements : les noms de nos listes que quelqu'un vient d'enregistrer.

- Zones ICANN : à chaque lecture, les noms entrés depuis la précédente. Un nom qui n'était pas cherché à la
  lecture précédente (liste ajoutée entre-temps) n'est pas un enregistrement du jour : il est écarté.
- .fr : la date de création du fichier Afnic. Une date nouvelle ou changée est un enregistrement ce jour-là ;
  au premier import, les créations de l'année écoulée remplissent le journal d'un coup.

Le contexte de chaque nom (score, catégories, demande ailleurs, avis IA) est ajouté après coup par enrich,
hors de la lecture de zone. Le journal ne mesure que la demande (quelqu'un a payé), pas un prix de revente.
"""

from datetime import date, datetime, timedelta

from . import ai, wordlists
from .checker import iso, utcnow

AFNIC_BACKFILL = timedelta(days=365)
ENRICH_BATCH = 5000


def journal_zone(conn, tld: str, labels, searched_before: set[str] | None, short, now: datetime) -> int:
    """Noms entrés dans la zone à cette lecture. `short` : noms courts (tous cherchés à chaque lecture) ;
    `labels` : noms des listes, gardés seulement s'ils étaient déjà cherchés à la lecture précédente."""
    kept = list(short) + [x for x in labels if searched_before is not None and x in searched_before]
    if not kept:
        return 0
    day = now.date().isoformat()
    removed = {}
    names = [f"{x}.{tld}" for x in kept]
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        removed.update(conn.execute(
            f"SELECT name, removed_on FROM drops WHERE name IN ({','.join('?' * len(part))})", part).fetchall())
    conn.executemany(
        "INSERT OR IGNORE INTO registrations (name, tld, label, day, source, removed_on) VALUES (?, ?, ?, ?, 'zone', ?)",
        [(name, tld, label, day, removed.get(name)) for name, label in zip(names, kept)])
    return len(kept)


def journal_afnic(conn, rows, today: date) -> int:
    """(label, création, suppression d'avant) des .fr dont la date de création est nouvelle."""
    since = today - AFNIC_BACKFILL
    found = [(f"{label}.fr", label, created.isoformat(), removed and removed.isoformat())
             for label, created, removed in rows if created and since <= created <= today]
    conn.executemany(
        "INSERT OR IGNORE INTO registrations (name, tld, label, day, source, removed_on) VALUES (?, 'fr', ?, ?, 'afnic', ?)",
        found)
    return len(found)


def enrich(explorer, limit: int = ENRICH_BATCH) -> int:
    """Contexte des enregistrements pas encore décrits : score, catégories, extensions où le nom était pris,
    avis IA. Par lots, pour ne pas tenir le NAS : la tâche suivante reprend où celle-ci s'arrête."""
    conn = explorer.conn
    rows = conn.execute("SELECT name, tld, label, day FROM registrations WHERE enriched_at IS NULL LIMIT ?", (limit,)).fetchall()
    if not rows:
        return 0
    u = explorer.universe
    tlds = explorer.tlds()
    data = {t: explorer.status_bytes(t) for t in tlds}
    contexts = {}
    opinions = ai.load(conn, [r["name"] for r in rows])
    now = iso(utcnow())
    out = []
    for r in rows:
        i = u.index(r["label"])
        score = taken = None
        if i is not None:
            taken = sum(1 for t in tlds if t != r["tld"] and data[t][i] in ai.TAKEN)
            if r["tld"] in data:
                ctx = contexts.setdefault(r["tld"], explorer._bonus_context(r["tld"]))
                score = explorer._value(r["tld"], i, r["label"], ctx)["score"]
        opinion = opinions.get(r["name"]) or {}
        if (opinion.get("rated_at") or "")[:10] > r["day"]:
            opinion = {}  # avis donné après l'enregistrement : ce n'est pas ce qu'on savait alors
        verdict = (opinion.get("comment") or "").split(".")[0] or None
        out.append((score, ",".join(wordlists.categories(r["label"])), taken, verdict, opinion.get("resale"), now, r["name"]))
    conn.executemany("UPDATE registrations SET score = ?, categories = ?, taken = ?, ai_verdict = ?, ai_resale = ?, "
                     "enriched_at = ? WHERE name = ? AND enriched_at IS NULL", out)
    return len(out)
