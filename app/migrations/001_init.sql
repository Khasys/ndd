CREATE TABLE domains (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL,
    state TEXT,
    state_since TEXT,
    check_error TEXT,
    epp_statuses TEXT NOT NULL DEFAULT '[]',
    registered_at TEXT,
    updated_at TEXT,
    expires_at TEXT,
    registrar TEXT,
    nameservers TEXT NOT NULL DEFAULT '[]',
    last_checked_at TEXT,
    last_success_at TEXT,
    next_check_at TEXT
);

CREATE INDEX idx_domains_next_check ON domains (next_check_at);

CREATE TABLE history (
    id INTEGER PRIMARY KEY,
    domain_id INTEGER NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);

CREATE INDEX idx_history_domain ON history (domain_id, at);
