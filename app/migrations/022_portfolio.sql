-- Portefeuille : noms possédés, achetés, en vente, vendus.
CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'owned',
    bought_on TEXT,
    buy_price INTEGER,
    renew_price INTEGER,
    expires_at TEXT,
    asking_price INTEGER,
    best_offer INTEGER,
    sold_on TEXT,
    sold_price INTEGER,
    notes TEXT,
    reminded TEXT,
    created_at TEXT NOT NULL
);
