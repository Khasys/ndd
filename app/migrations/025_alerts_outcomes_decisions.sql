-- Alertes immédiates : une par nom et par état (free, coming), pour ne jamais prévenir deux fois.
CREATE TABLE IF NOT EXISTS alert_sent (
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (name, kind)
);
-- Mesure a posteriori des avis IA : code d'état de l'Explorer au moment de l'avis.
ALTER TABLE domain_ai ADD COLUMN status_at INTEGER;
-- Noms notés libres il y a 30 jours au moins : repris par quelqu'un d'autre depuis, ou pas.
CREATE TABLE IF NOT EXISTS ai_outcomes (
    name TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,
    resale INTEGER NOT NULL,
    rated_at TEXT NOT NULL,
    free_at_rating INTEGER NOT NULL,
    taken_at TEXT,
    checked_at TEXT NOT NULL
);
-- Décisions gardées : ignoré (Veille) ou acheté (portefeuille), pour mesurer les regrets.
CREATE TABLE IF NOT EXISTS decisions (
    name TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    at TEXT NOT NULL,
    note TEXT
);
