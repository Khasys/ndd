-- Noms écartés à la main de l'écran Suppressions (tri en masse) : ils restent dans l'Explorer.
CREATE TABLE IF NOT EXISTS drop_hidden (
    name TEXT PRIMARY KEY,
    hidden_at TEXT NOT NULL
);
