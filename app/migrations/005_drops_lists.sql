-- Listes d'origine d'un nom suivi : mot, nom de famille. Vide pour un nom court seul.
ALTER TABLE drops ADD COLUMN lists TEXT NOT NULL DEFAULT '';
