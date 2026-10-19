-- Carte « Premiums probables » retirée : prix réels et premiums confirmés viennent de Gandi ; .com et .net
-- restent les seules extensions sans premium. Les anciens réglages ne servent plus.
DELETE FROM settings WHERE key IN ('premium_prices', 'premium_devalue', 'premium_off');
