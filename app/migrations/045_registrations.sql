-- Journal des enregistrements : chaque nom de nos listes qui entre dans une zone (ou que l'Afnic date d'une
-- création), avec ce qu'on savait de lui. Matière des statistiques : quels noms libres partent, et en combien
-- de temps. day : jour de la lecture de zone (enregistré depuis la lecture précédente) ou date de création .fr.
-- source : zone ou afnic. removed_on : date de suppression d'avant (nom repris après sa libération), sinon vide.
-- Contexte (rempli après coup, enriched_at) : score, catégories, extensions où le nom était déjà pris,
-- verdict et note de revente de l'avis IA s'il existait.
CREATE TABLE IF NOT EXISTS registrations (
    name TEXT NOT NULL,
    tld TEXT NOT NULL,
    label TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    removed_on TEXT,
    score INTEGER,
    categories TEXT,
    taken INTEGER,
    ai_verdict TEXT,
    ai_resale INTEGER,
    enriched_at TEXT,
    PRIMARY KEY (name, day)
);
CREATE INDEX IF NOT EXISTS registrations_day ON registrations (day, tld);
CREATE INDEX IF NOT EXISTS registrations_enrich ON registrations (enriched_at);
