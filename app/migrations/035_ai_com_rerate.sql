-- Consigne IA corrigée : un mot français en .com n'est plus pénalisé (adéquation à l'extension, prononciation).
-- Les avis .com déjà notés repassent dans la file (version d'avant) : la tâche les renote avec la nouvelle consigne.
UPDATE domain_ai SET version = 4 WHERE name LIKE '%.com' AND version >= 5;
