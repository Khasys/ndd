-- Entreprises (app/companies.py) : les plus grandes entreprises françaises et l'état de leurs noms en .fr et .com,
-- pour un audit de présence en ligne. À part des listes, de la Veille et de la notation IA.
CREATE TABLE IF NOT EXISTS companies (
    siren TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sigle TEXT,
    commercial TEXT NOT NULL DEFAULT '[]',  -- nom commercial et enseignes du siège (JSON)
    extra TEXT NOT NULL DEFAULT '[]',       -- noms ajoutés à la main (JSON)
    category TEXT,                          -- GE, ETI, PME (Insee)
    headcount TEXT,                         -- tranche d'effectif (code Insee)
    headcount_year TEXT,
    revenue INTEGER,                        -- dernier chiffre d'affaires publié
    revenue_year TEXT,
    net_income INTEGER,
    naf TEXT,
    city TEXT,
    postcode TEXT,
    siret TEXT,                             -- siège
    created_on TEXT,
    rank INTEGER,                           -- place dans le top, vide pour une entreprise ajoutée à la main
    added_at TEXT,                          -- ajoutée depuis la recherche
    updated_at TEXT NOT NULL,
    folded TEXT NOT NULL DEFAULT ''         -- noms en minuscules sans accents, pour la recherche
);
CREATE INDEX IF NOT EXISTS companies_rank ON companies (rank);

CREATE TABLE IF NOT EXISTS company_domains (
    name TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    tld TEXT NOT NULL,
    state TEXT,                             -- taken, free, ou vide : pas encore vu
    created_on TEXT,
    expires_on TEXT,
    registrar TEXT,
    source TEXT,                            -- rdap, afnic AAAAMM
    checked_at TEXT,
    error TEXT
);
