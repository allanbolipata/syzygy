ALTER TABLE pairing_logic
ADD COLUMN strategy_name VARCHAR(100) NOT NULL DEFAULT 'default';

ALTER TABLE pairing_logic
ADD COLUMN config JSONB;
