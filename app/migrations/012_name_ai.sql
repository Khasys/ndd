-- Avis IA sur un nom (sans extension) : notes sur 100, fourchette de prix en euros, commentaire court.
CREATE TABLE name_ai (
    label TEXT PRIMARY KEY,
    exploit INTEGER NOT NULL,
    resale INTEGER NOT NULL,
    price_low INTEGER,
    price_high INTEGER,
    comment TEXT NOT NULL,
    rated_at TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE INDEX name_ai_resale ON name_ai (resale DESC, exploit DESC)
