-- Analyse SEO à la demande : Open PageRank (liens) et Wayback Machine (historique du site).
CREATE TABLE domain_seo (
    name TEXT PRIMARY KEY,
    pr REAL,
    wb_first TEXT,
    wb_last TEXT,
    wb_years INTEGER,
    checked_at TEXT NOT NULL,
    error TEXT
);
