-- Accès CZDS accordé au .com et au .net : la zone .com est lue comme les autres zones ICANN. Le mécanisme
-- provisoire (.com par Gandi seul, passe continue Gandi jour et nuit, passe DNS sur les serveurs du .com) est retiré.
-- Noms .com interrogés dans le DNS : la zone fait l'état. Les .co, .ai et .io gardent leurs lignes.
DELETE FROM cc_dns WHERE name LIKE '%.com';
-- Réglages et états du mécanisme retiré : extensions par Gandi seul, passe continue, revérifications « À prendre »,
-- passe DNS .com.
DELETE FROM settings WHERE key IN (
    'gandi_only_tlds',
    'gandi_live_enabled', 'gandi_live_rate', 'gandi_live_days', 'gandi_live_status', 'gandi_live_halt',
    'gandi_recheck',
    'com_dns_enabled', 'com_dns_last', 'com_dns_cap', 'com_dns_status'
);
-- Ancienne marque « Gandi ne vend pas le .com » (ignorée tant que le .com était par Gandi seul) : effacée, sinon la
-- passe de nuit écarterait le .com.
UPDATE settings SET value = json_remove(value, '$.com')
WHERE key = 'gandi_unsold' AND json_valid(value) AND json_type(value, '$.com') IS NOT NULL;
