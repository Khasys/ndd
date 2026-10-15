-- Présence d'un nom .fr dans le DNS, vue directement sur les serveurs du .fr (pas de quota RDAP).
CREATE TABLE name_dns (
    label TEXT PRIMARY KEY,
    present INTEGER NOT NULL,
    checked_at TEXT NOT NULL
)
