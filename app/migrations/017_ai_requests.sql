-- Analyses IA demandées depuis la fiche d'un nom : traitées par la tâche horaire.
CREATE TABLE IF NOT EXISTS ai_requests (
    name TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL,
    done_at TEXT,
    seen_at TEXT
);
