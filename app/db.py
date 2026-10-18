import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
KEEP_BACKUPS = 10
# Attente d'un autre fil qui écrit, en secondes. Les écritures sont courtes : c'est un filet.
BUSY_TIMEOUT = 30


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


class ThreadConnections:
    """Une connexion SQLite par fil, derrière un seul objet.

    La boucle principale, le fil de l'Explorer et les fils des run_in_executor travaillent en même
    temps. Une connexion partagée mêle leurs requêtes et leurs transactions (« bad parameter or other
    API misuse », tâches planifiées qui plantent). Chacun a donc la sienne ; en WAL, les lectures se
    font en parallèle et les écritures passent l'une après l'autre."""

    def __init__(self, path: Path):
        self._path = path
        self._local = threading.local()
        self._conn()  # erreur d'ouverture tout de suite, pas à la première requête

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = _open(self._path)
        return conn

    def __getattr__(self, name):
        return getattr(self._conn(), name)


def connect(path: Path) -> ThreadConnections:
    return ThreadConnections(path)


def _migrations() -> list[tuple[int, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        found.append((int(path.name.split("_", 1)[0]), path))
    return found


def backup(conn: sqlite3.Connection, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"domain-watch-{stamp}.db"
    dest = sqlite3.connect(target)
    with dest:
        conn.backup(dest)
    dest.close()
    for old in sorted(backup_dir.glob("domain-watch-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    return target


def _execute(conn: sqlite3.Connection, statement: str) -> None:
    try:
        conn.execute(statement)
    except sqlite3.OperationalError as exc:
        # Une migration est idempotente ; SQLite n'a pas de « ADD COLUMN IF NOT EXISTS ».
        if "duplicate column name" not in str(exc) or "ADD COLUMN" not in statement.upper():
            raise


def migrate(conn: sqlite3.Connection, backup_dir: Path) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = [(v, p) for v, p in _migrations() if v > current]
    if not pending:
        return
    # Pas de sauvegarde à la création : une base vide n'a rien à protéger.
    if current > 0:
        log.info("Sauvegarde avant migration : %s", backup(conn, backup_dir))
    for version, path in pending:
        log.info("Migration %s", path.name)
        conn.execute("BEGIN")
        try:
            # Les lignes de commentaire « -- » partent d'abord : un point-virgule dans l'une d'elles casserait le découpage.
            sql = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("--"))
            for statement in sql.split(";"):
                if statement.strip():
                    _execute(conn, statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
