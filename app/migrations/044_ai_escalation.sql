-- Escalade des avis IA, à la place de la contre-notation (la plus basse de deux notes) : un « À surveiller »
-- prometteur reçoit deux contre-avis (repêchage) ; tout « À prendre » passe devant un panel de dix correcteurs.
-- stage : rescue, panel, done. claim : passage de la tâche qui a reçu le nom, réservé jusqu'à claimed_until.
CREATE TABLE IF NOT EXISTS ai_escalation (
    name TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    claimed_until TEXT,
    claim TEXT,
    notify INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    done_at TEXT
);
-- Un avis par correcteur et par étape ; un envoi répété du même correcteur ne compte qu'une fois.
CREATE TABLE IF NOT EXISTS ai_votes (
    name TEXT NOT NULL,
    stage TEXT NOT NULL,
    claim TEXT NOT NULL,
    voter INTEGER NOT NULL,
    item TEXT NOT NULL,
    verdict TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (name, stage, claim, voter)
);
-- Décision du panel gardée avec l'avis : nombre de « À prendre » sur dix, veto, dispersion.
ALTER TABLE domain_ai ADD COLUMN panel TEXT;
-- Rattrapage : un « À prendre » actuel ou déjà contre-noté (il l'a donc été) passe au panel ;
-- un « À surveiller » qui rapporte au moins 5 euros par an passe au repêchage.
INSERT OR IGNORE INTO ai_escalation (name, stage, queued_at)
    SELECT name, 'panel', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') FROM domain_ai
    WHERE version >= 7 AND (comment LIKE 'À prendre.%' OR confirmed_at IS NOT NULL);
INSERT OR IGNORE INTO ai_escalation (name, stage, queued_at)
    SELECT name, 'rescue', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') FROM domain_ai
    WHERE version >= 7 AND comment LIKE 'À surveiller.%' AND yield >= 5;
