-- D'où vient une ligne de la Veille : NULL, sortie de zone ou du DNS (une vraie libération) ; 'manual', bouton
-- Vérifier d'un nom jamais sorti ; 'gandi', revérification au registre d'un nom refusé à l'achat. Seules les
-- premières sont des libérations : les autres ne s'affichent ni dans la Veille ni dans Aujourd'hui.
ALTER TABLE drops ADD COLUMN origin TEXT;
UPDATE drops SET origin = 'manual' WHERE removed_on = '';
