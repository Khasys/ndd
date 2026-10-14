-- Avis IA par nom complet (velo.fr) : remplace l'avis par mot, jamais utilisé en production.
DROP TABLE IF EXISTS name_ai;
CREATE TABLE domain_ai (
    name TEXT PRIMARY KEY,
    exploit INTEGER NOT NULL,
    resale INTEGER NOT NULL,
    quick_low INTEGER NOT NULL,
    quick_high INTEGER NOT NULL,
    final_low INTEGER NOT NULL,
    final_high INTEGER NOT NULL,
    comment TEXT NOT NULL,
    rated_at TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE INDEX domain_ai_quick ON domain_ai (quick_high DESC, resale DESC)
