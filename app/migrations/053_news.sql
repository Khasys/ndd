-- Radar des nouveautés (news.py) : mots, expressions et événements de l'actualité proposés par la tâche IA deux fois
-- par jour, vérifiés au registre en .fr et en .com, notés par l'IA quand ils sont libres.
-- fr, com : état au registre à la vérification (free, coming, taken, reserved, premium, unknown), NULL avant.
CREATE TABLE IF NOT EXISTS news (
    label TEXT PRIMARY KEY,
    lang TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT,
    added_at TEXT NOT NULL,
    fr TEXT,
    com TEXT,
    checked_at TEXT,
    tries INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS news_added ON news (added_at);
-- Demande d'analyse IA posée par le radar (source news) : ni dans la cloche de l'appli, ni message « Analyse IA prête ».
ALTER TABLE ai_requests ADD COLUMN source TEXT;
