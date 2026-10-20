-- Contre-notation : un avis « À prendre » est renoté à l'aveugle par un second correcteur, la note la plus basse
-- reste. confirmed_at : date de ce second avis (vide tant qu'il n'a pas eu lieu).
ALTER TABLE domain_ai ADD COLUMN confirmed_at TEXT;
