-- File de rejeu Telegram : un message refusé (429, 5xx) ou parti dans le vide (réseau) attend ici
-- son prochain essai, au lieu d'être perdu. Les messages de test n'y passent pas.
CREATE TABLE IF NOT EXISTS telegram_outbox (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at TEXT NOT NULL,
    last_error TEXT
);
