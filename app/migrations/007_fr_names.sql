-- Noms .fr suivis, issus de l'open data de l'Afnic, et leur échéance relevée en RDAP.
CREATE TABLE fr_names (
    label TEXT PRIMARY KEY,
    created_on TEXT,
    removed_on TEXT,
    expires_at TEXT,
    checked_at TEXT,
    next_check_at TEXT
);

CREATE INDEX fr_names_next_check ON fr_names (next_check_at);
