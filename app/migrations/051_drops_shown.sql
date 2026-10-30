-- Index des noms affichés dans la Veille et Aujourd'hui : sans les 800 000 libres de la passe des absents
-- (origine rdap), qui faisaient relire toute la table à chaque ouverture (jusqu'à 25 s au NAS, 30/10/2026).
-- Même condition que drops.QUIET_SQL, à l'identique : sinon SQLite n'emploie pas l'index.
CREATE INDEX IF NOT EXISTS drops_shown ON drops (state, available_since) WHERE COALESCE(origin, '') NOT IN ('gandi', 'rdap');
-- Suspendus par extension (ligne des sources de la Veille) : quelques milliers de lignes, sans parcourir la table.
CREATE INDEX IF NOT EXISTS drops_suspended ON drops (tld) WHERE state = 'suspended';
