-- Ventes réelles du marché secondaire (rapports DNJournal, lus par le NAS) : comparables pour l'avis IA et la
-- fiche. Prix en dollars, tels que publiés ; date : fin de la période couverte par le rapport.
CREATE TABLE IF NOT EXISTS market_sales (
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    date TEXT NOT NULL,
    venue TEXT,
    source TEXT,
    PRIMARY KEY (name, date)
);
CREATE TABLE IF NOT EXISTS market_reports (
    url TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    sales INTEGER NOT NULL
);
