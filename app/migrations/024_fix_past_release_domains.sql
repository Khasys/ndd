-- Domaines surveillés vus pour la première fois en rédemption ou en suppression : le début de la
-- phase était pris sur la date de modification RDAP même quand elle précédait l'échéance (date
-- ancienne, sans rapport), d'où des libérations estimées déjà passées. Ces domaines repassent en
-- première vérification (tout de suite) : l'état et son début sont relus avec le garde-fou.
UPDATE domains SET state = NULL, state_since = NULL, next_check_at = last_checked_at
WHERE state IN ('redemption', 'pending_delete') AND state_since IS NOT NULL
  AND state_since < strftime('%Y-%m-%dT%H:%M:%S', 'now',
        CASE WHEN name LIKE '%.fr' THEN '-30 days' WHEN state = 'pending_delete' THEN '-5 days' ELSE '-35 days' END);
