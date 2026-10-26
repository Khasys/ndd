-- Avertissements et erreurs gardés 7 jours (requêtes lentes, boucle bloquée, erreurs) : la cause d'une 502 du proxy
-- reste lisible après coup, quand les 50 lignes en mémoire de /api/diag sont déjà passées.
CREATE TABLE IF NOT EXISTS journal (
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_at ON journal (at);
