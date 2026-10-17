-- Libérations estimées déjà passées pour des noms encore en rédemption ou en suppression : début de
-- rédemption pris sur une vieille date de modification RDAP. Ces noms repassent en vérification
-- (prochaine lecture RDAP tout de suite) et leur estimation est recalculée.
UPDATE drops SET state = NULL, state_since = NULL, release_at = NULL, next_check_at = checked_at
WHERE state IN ('redemption', 'pending_delete') AND release_at IS NOT NULL
  AND release_at < strftime('%Y-%m-%dT%H:%M:%S', 'now');
