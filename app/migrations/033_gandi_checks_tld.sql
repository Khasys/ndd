-- Extension de chaque réponse de Gandi, tirée du nom (colonne calculée, rien à écrire) : avec le .com vérifié par
-- Gandi seul, la table compte des centaines de milliers de lignes, et chaque extension lit les siennes par l'index
-- au lieu de parcourir toute la table (name LIKE '%.fr').
ALTER TABLE gandi_checks ADD COLUMN tld TEXT GENERATED ALWAYS AS (substr(name, instr(name, '.') + 1)) VIRTUAL;
CREATE INDEX IF NOT EXISTS gandi_checks_tld ON gandi_checks (tld, kind);
