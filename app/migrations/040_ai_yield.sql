-- Rendement attendu de l'avis IA : demande mesurée au moment de l'avis (autres extensions où le nom est pris),
-- renouvellement de l'extension (euros par an), chance de vente par an et rendement (euros par an) calculés.
ALTER TABLE domain_ai ADD COLUMN taken INTEGER;
ALTER TABLE domain_ai ADD COLUMN renewal REAL;
ALTER TABLE domain_ai ADD COLUMN sell_rate REAL;
ALTER TABLE domain_ai ADD COLUMN yield REAL;
