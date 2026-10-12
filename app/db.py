import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
KEEP_BACKUPS = 10


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


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
            for statement in path.read_text().split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
