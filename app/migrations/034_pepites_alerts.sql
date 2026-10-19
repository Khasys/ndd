-- Pépites : une ligne par nom vu pépite (avis « À prendre » au seuil, libre ou bientôt libre), pour dater
-- l'événement et ne jamais prévenir deux fois. state : free, unconfirmed, premium, coming, gone (plus pépite
-- pour l'instant) ou taken (repris : après 7 jours, sa prochaine libération est un nouvel événement).
CREATE TABLE IF NOT EXISTS pepites (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    free_at TEXT,
    coming_at TEXT,
    release_at TEXT,
    seen_at TEXT NOT NULL,
    taken_at TEXT,
    alerted_at TEXT,
    daily_at TEXT,
    daily_release TEXT,
    daily_soon INTEGER NOT NULL DEFAULT 0
);
-- Réglages d'alerte d'avant, repris dans la carte unique « Alertes » : une case par type, le seuil des pépites,
-- l'heure du quotidien (celle du résumé du matin), le jour du résumé de la semaine, le score du récapitulatif.
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_on_pepite_free', value FROM settings WHERE key = 'alert_take_enabled';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_on_pepite_coming', value FROM settings WHERE key = 'alert_take_enabled';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_pepite_min', value FROM settings WHERE key = 'alert_take_min';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_on_morning', value FROM settings WHERE key = 'morning_enabled';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_daily_hour', value FROM settings WHERE key = 'morning_hour';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_morning_min', value FROM settings WHERE key = 'morning_min';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_on_weekly', value FROM settings WHERE key = 'digest_enabled';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_weekly_day', value FROM settings WHERE key = 'digest_day';
-- Le résumé du matin déjà parti aujourd'hui vaut pour le quotidien : rien ne repart le jour de la mise à jour.
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_daily_last', value FROM settings WHERE key = 'morning_last_sent';
INSERT OR IGNORE INTO settings (key, value) SELECT 'alert_weekly_last', value FROM settings WHERE key = 'digest_last_sent';
DELETE FROM settings WHERE key IN ('alert_take_enabled', 'alert_take_min', 'morning_enabled', 'morning_hour', 'morning_min',
    'digest_enabled', 'digest_day', 'digest_hour', 'morning_last_sent', 'digest_last_sent', 'view_alert_run');
