-- Compteurs de la ligne .fr (Veille, Paramètres) : noms enregistrés par date de prochaine vérification.
-- L'index couvre la condition et la date, la table de 230 000 noms n'est plus parcourue à chaque affichage.
CREATE INDEX IF NOT EXISTS fr_names_due ON fr_names (removed_on, next_check_at);
