-- Date de sortie vide (ligne de Veille posée à la main, drops.MANUAL) recopiée dans le journal : pas de sortie.
UPDATE registrations SET removed_on = NULL WHERE removed_on = '';
