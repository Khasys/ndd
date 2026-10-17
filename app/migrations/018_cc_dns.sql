-- Présence DNS des .co, .ai et .io (pas de fichier de zone pour ces extensions).
CREATE TABLE IF NOT EXISTS cc_dns (
    name TEXT PRIMARY KEY,
    present INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    seen_at TEXT,
    lost_at TEXT
);
