-- Écran Entreprises retiré (v0.14.1, 27/10/2026) : ses tables et son état effacés.
DROP TABLE IF EXISTS company_domains;
DROP TABLE IF EXISTS companies;
DELETE FROM settings WHERE key LIKE 'companies%';
