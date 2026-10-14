-- Journal des requêtes RDAP vers l'Afnic : plafond commun, travail du jour, suivi en direct.
CREATE TABLE fr_requests (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    outcome TEXT NOT NULL
);

CREATE INDEX fr_requests_at ON fr_requests (at);
CREATE INDEX drops_tld_next ON drops (tld, next_check_at);
CREATE INDEX fr_names_removed ON fr_names (removed_on);
