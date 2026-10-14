-- Noms qu'on ne peut pas simplement réserver : termes Afnic soumis à examen préalable, noms de
-- communes protégés, et marquages faits à la main (réservé ou, au contraire, pas réservé).
CREATE TABLE reserved_names (
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (name, source)
);

CREATE TABLE name_flags (
    name TEXT PRIMARY KEY,
    reserved INTEGER NOT NULL,
    at TEXT NOT NULL
);
