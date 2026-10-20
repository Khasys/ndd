-- Jetons d'accès à l'appli, plusieurs par type, avec un libellé et la date de dernière utilisation.
-- Seule l'empreinte (SHA-256) est gardée. Les jetons uniques d'avant (notation IA, connecteur MCP) sont repris :
-- la tâche de notation et un connecteur déjà branchés continuent de marcher.
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
INSERT INTO tokens (kind, label, hash, created_at)
    SELECT 'ai', 'Tâche de notation', value, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
    FROM settings WHERE key = 'ai_token_hash' AND value != '';
INSERT INTO tokens (kind, label, hash, created_at)
    SELECT 'mcp', 'Connecteur MCP', value, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
    FROM settings WHERE key = 'mcp_token_hash' AND value != '';
DELETE FROM settings WHERE key IN ('ai_token_hash', 'mcp_token_hash');
