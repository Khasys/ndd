CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

ALTER TABLE domains ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
ALTER TABLE domains ADD COLUMN signals TEXT;
ALTER TABLE domains ADD COLUMN signals_checked_at TEXT
