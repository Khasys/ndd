-- Gandi, source d'information seulement : état et prix de création d'un nom non pris, lus par /v5/domain/check.
-- status : statut brut de Gandi (available, unavailable, unavailable_premium...) ; kind : available, premium,
-- unavailable, reserved, restricted ou error. price : prix TTC d'un an de création, en euros (price_ht hors taxes).
CREATE TABLE IF NOT EXISTS gandi_checks (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    price REAL,
    price_ht REAL,
    currency TEXT,
    checked_at TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS gandi_checks_kind ON gandi_checks (kind);
