import sqlite3


class Store:
    """Réglages modifiables depuis l'interface, rangés dans la base."""

    def __init__(self, conn: sqlite3.Connection, defaults: dict[str, str] | None = None):
        self.conn = conn
        # Valeurs issues des variables d'environnement, utilisées tant que rien n'est enregistré.
        self.defaults = defaults or {}

    def get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row[0]
        return self.defaults.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
