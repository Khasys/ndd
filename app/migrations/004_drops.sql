-- Noms de 4 caractères sortis des zones ICANN, suivis jusqu'à leur libération.
CREATE TABLE drops (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    tld TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    removed_on TEXT NOT NULL,
    state TEXT,
    state_since TEXT,
    release_at TEXT,
    available_since TEXT,
    checked_at TEXT,
    next_check_at TEXT,
    error TEXT
);

CREATE INDEX drops_next_check ON drops (next_check_at);
