DROP TABLE IF EXISTS name_price;
DELETE FROM settings WHERE key LIKE 'gandi\_%' ESCAPE '\'
