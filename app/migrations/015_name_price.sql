CREATE TABLE name_price (
    name TEXT PRIMARY KEY,
    available INTEGER,
    status TEXT,
    price REAL,
    normal REAL,
    currency TEXT,
    premium INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL,
    error TEXT,
    raw TEXT
);
