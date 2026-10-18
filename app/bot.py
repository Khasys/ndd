from datetime import datetime

from . import states
from .checker import Checker, DuplicateDomain, parse_iso, utcnow
from .digest import local_zone
from .rdap import InvalidDomain, clean_domain

HELP = """Commandes :
/liste : tous les domaines
/urgent : domaines hors état actif
/ajouter nom.fr : surveiller un domaine
/supprimer nom.fr : arrêter la surveillance
/verifier nom.fr : vérifier maintenant
/statut : résumé et version"""

URGENT = [s for s in states.URGENCY if s != states.ACTIVE]


def _line(checker: Checker, row, now: datetime) -> str:
    label = "Erreur de check" if row["check_error"] else states.LABELS.get(row["state"], "Non vérifié")
    parts = [row["name"], label]
    expiry = states.effective_expiry(row["state"], parse_iso(row["expires_at"]), now)
    if expiry and row["state"] != states.AVAILABLE:
        parts.append(f"{(expiry - now).days} j")
    release = checker.release_estimate(row, now)
    if release:
        parts.append(f"libération ~{release.astimezone(local_zone()).strftime('%d/%m/%Y')}")
    return " · ".join(parts)


def _sorted(rows):
    order = {s: i for i, s in enumerate(states.URGENCY)}
    return sorted(rows, key=lambda r: (order.get(r["state"], len(order)), r["name"]))


def build_handler(checker: Checker, version: str):
    conn = checker.conn

    def find(argument: str):
        try:
            name = clean_domain(argument)
        except InvalidDomain as exc:
            return None, str(exc)
        row = conn.execute("SELECT * FROM domains WHERE name = ?", (name,)).fetchone()
        return row, None if row else f"{name} n'est pas surveillé."

    async def handle(command: str, argument: str) -> str:
        now = utcnow()
        if command in ("/start", "/aide", "/help"):
            return HELP

        if command in ("/liste", "/urgent"):
            rows = conn.execute("SELECT * FROM domains").fetchall()
            if command == "/urgent":
                rows = [r for r in rows if r["state"] in URGENT or r["check_error"]]
            if not rows:
                return "Aucun domaine." if command == "/liste" else "Rien d'urgent."
            lines = [_line(checker, r, now) for r in _sorted(rows)]
            # Telegram refuse les messages de plus de 4096 caractères.
            text = "\n".join(lines)
            return text if len(text) < 4000 else text[:3990] + "\n[liste tronquée]"

        if command == "/ajouter":
            if not argument:
                return "Usage : /ajouter nom.fr"
            try:
                row = await checker.add(argument)
            except (InvalidDomain, DuplicateDomain) as exc:
                return str(exc)
            if row["check_error"]:
                return f"{row['name']} ajouté. Premier check en échec : {row['check_error']}."
            return "Ajouté : " + _line(checker, row, now)

        if command == "/supprimer":
            row, error = find(argument)
            if error:
                return error
            conn.execute("DELETE FROM domains WHERE id = ?", (row["id"],))
            return f"{row['name']} supprimé."

        if command == "/verifier":
            row, error = find(argument)
            if error:
                return error
            await checker.check(row["id"])
            row = conn.execute("SELECT * FROM domains WHERE id = ?", (row["id"],)).fetchone()
            suffix = f" ({row['check_error']})" if row["check_error"] else ""
            return _line(checker, row, now) + suffix

        if command == "/statut":
            rows = conn.execute("SELECT state, check_error FROM domains").fetchall()
            counts: dict[str, int] = {}
            for r in rows:
                key = "Erreur de check" if r["check_error"] else states.LABELS.get(r["state"], "Non vérifié")
                counts[key] = counts.get(key, 0) + 1
            detail = "\n".join(f"{k} : {v}" for k, v in sorted(counts.items()))
            return f"{len(rows)} domaines surveillés, version {version}\n{detail}".strip()

        return "Commande inconnue. /aide pour la liste."

    return handle
