-- Dernière réponse RDAP ou WHOIS pour un nom hors de la liste Domaines (Explorer, Suppressions, .fr).
CREATE TABLE name_rdap (
    name TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    available INTEGER NOT NULL,
    statuses TEXT,
    registrar TEXT,
    registered_at TEXT,
    updated_at TEXT,
    expires_at TEXT,
    nameservers TEXT
);
