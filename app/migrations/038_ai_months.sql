-- Délais de vente médians estimés par l'IA, en mois : revente rapide à un investisseur et achat immédiat au prix
-- utilisateur final (type Afternic). Vides pour les avis d'avant (version 5).
ALTER TABLE domain_ai ADD COLUMN quick_months INTEGER;
ALTER TABLE domain_ai ADD COLUMN final_months INTEGER;
