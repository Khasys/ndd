-- Avis IA des extensions qui ne sont plus notées (.biz, .info), tirés de la nuit d'essai et d'une
-- consigne plus ancienne (dont des noms réservés notés « À prendre »).
DELETE FROM domain_ai WHERE name LIKE '%.biz' OR name LIKE '%.info';
