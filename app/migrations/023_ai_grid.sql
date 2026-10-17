-- Grille IA v5 : sous-notes de l'avis (JSON) et date du second passage de contrôle.
ALTER TABLE domain_ai ADD COLUMN grid TEXT;
ALTER TABLE domain_ai ADD COLUMN reviewed_at TEXT;
