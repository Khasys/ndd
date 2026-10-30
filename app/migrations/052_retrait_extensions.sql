-- .app, .biz, .dev, .info et .xyz retirés le 31/10/2026 (drops.RETIRED_TLDS) : ce que l'appli en avait appris est
-- effacé. Les domaines suivis, le portefeuille, les décisions et les noms marqués réservés sont gardés.
DELETE FROM drops WHERE tld IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM zone_scans WHERE tld IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM registrations WHERE tld IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM gandi_checks WHERE tld IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM name_rdap WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM domain_ai WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM ai_outcomes WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM ai_requests WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM ai_votes WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM ai_escalation WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM alert_sent WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM pepites WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM domain_seo WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM exploit WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM drop_hidden WHERE substr(name, instr(name, '.') + 1) IN ('app', 'biz', 'dev', 'info', 'xyz');
DELETE FROM settings WHERE key IN ('zone_app', 'zone_lists_app', 'zone_biz', 'zone_lists_biz', 'zone_dev', 'zone_lists_dev', 'zone_info', 'zone_lists_info', 'zone_xyz', 'zone_lists_xyz');
-- Noms de la passe des absents vus en rédemption ou en suppression : des libérations à venir, dans la Veille et
-- Aujourd'hui comme une sortie de zone (drops.SEEN_ABSENT). Les libres et les enregistrés restent discrets.
UPDATE drops SET origin = 'absent' WHERE origin = 'rdap' AND state IN ('expired', 'redemption', 'pending_delete');
