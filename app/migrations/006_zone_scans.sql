-- Historique des lectures de zones : durée, part du téléchargement et du calcul.
CREATE TABLE zone_scans (
    id INTEGER PRIMARY KEY,
    tld TEXT NOT NULL,
    at TEXT NOT NULL,
    megabytes REAL NOT NULL,
    seconds REAL NOT NULL,
    wait_seconds REAL NOT NULL,
    compute_seconds REAL NOT NULL,
    workers INTEGER NOT NULL,
    names INTEGER,
    removed INTEGER,
    added INTEGER,
    error TEXT
);

CREATE INDEX zone_scans_tld_at ON zone_scans (tld, at);
