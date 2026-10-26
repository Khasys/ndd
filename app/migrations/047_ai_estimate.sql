-- Estimation du score gardée avec l'avis IA : le second passage ne la recalcule plus pour chaque avis (file IA lente).
ALTER TABLE domain_ai ADD COLUMN estimate INTEGER;
