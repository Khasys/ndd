"""Jetons d'accès à l'appli : plusieurs par type, avec un libellé, révocables un à un.

Seule l'empreinte est gardée ; le jeton est montré une fois, à la création. La date de dernière utilisation est
notée (au plus toutes les 10 minutes) : un jeton qui ne sert plus se repère et se révoque.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

# Type : (libellé, préfixe du jeton, ce qu'il ouvre).
KINDS = {
    "ai": ("Notation IA", "dw_", "liste à noter, envoi des avis et lectures"),
    "read": ("Lecture seule", "dwr_", "lectures de l'API (écrans, fiches, diagnostic)"),
    "mcp": ("Connecteur MCP", "dwm_", "ChatGPT ou Claude, en lecture seule"),
}
MAX_TOKENS = 30
LABEL_MAX = 60
TOUCH_EVERY = timedelta(minutes=10)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_label(label: str | None, kind: str) -> str:
    label = " ".join((label or "").split())[:LABEL_MAX]
    return label or KINDS[kind][0]


def create(conn, kind: str, label: str | None = None) -> tuple[int, str]:
    """(id, jeton en clair). ValueError : type inconnu ou trop de jetons."""
    if kind not in KINDS:
        raise ValueError("Type de jeton inconnu.")
    if conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] >= MAX_TOKENS:
        raise ValueError(f"{MAX_TOKENS} jetons au plus : révoque ceux qui ne servent plus.")
    token = KINDS[kind][1] + secrets.token_urlsafe(32)
    cur = conn.execute("INSERT INTO tokens (kind, label, hash, created_at) VALUES (?, ?, ?, ?)",
                       (kind, clean_label(label, kind), hash_token(token), _now().isoformat(timespec="seconds")))
    return cur.lastrowid, token


def check(conn, kinds, token: str | None) -> int | None:
    """Id du jeton s'il est valide pour l'un des types demandés, sinon None. Note la dernière utilisation."""
    if not token:
        return None
    row = conn.execute("SELECT id, kind FROM tokens WHERE hash = ?", (hash_token(token.strip()),)).fetchone()
    if row is None or row[1] not in kinds:
        return None
    now = _now()
    conn.execute("UPDATE tokens SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
                 (now.isoformat(timespec="seconds"), row[0], (now - TOUCH_EVERY).isoformat(timespec="seconds")))
    return row[0]


def bearer(header: str | None) -> str | None:
    return header[7:] if header and header.startswith("Bearer ") else None


def any_of(conn, kind: str) -> bool:
    return conn.execute("SELECT 1 FROM tokens WHERE kind = ? LIMIT 1", (kind,)).fetchone() is not None


def listing(conn) -> list[dict]:
    rows = conn.execute("SELECT id, kind, label, created_at, last_used_at FROM tokens ORDER BY created_at DESC, id DESC")
    return [{"id": r[0], "kind": r[1], "kind_label": KINDS.get(r[1], (r[1],))[0], "label": r[2], "created_at": r[3],
             "last_used_at": r[4]} for r in rows]


def rename(conn, token_id: int, label: str | None) -> bool:
    row = conn.execute("SELECT kind FROM tokens WHERE id = ?", (token_id,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE tokens SET label = ? WHERE id = ?", (clean_label(label, row[0]), token_id))
    return True


def revoke(conn, token_id: int) -> bool:
    return bool(conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,)).rowcount)


def revoke_kind(conn, kind: str) -> None:
    conn.execute("DELETE FROM tokens WHERE kind = ?", (kind,))
