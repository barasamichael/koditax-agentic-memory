BEGIN;

UPDATE computations
SET regime_type = 'health_contribution'
WHERE regime_type = 'health_tax';

ALTER TABLE computations
    DROP CONSTRAINT IF EXISTS chk_computations_health_tax_regime_identifier;

ALTER TABLE computations
    DROP CONSTRAINT IF EXISTS chk_computations_regime_type;

ALTER TABLE computations
    ADD CONSTRAINT chk_computations_regime_type CHECK (
        regime_type IN ('income_tax', 'health_contribution', 'vat', 'other')
    );

ALTER TABLE computations
    ADD CONSTRAINT chk_computations_health_contribution_regime_identifier CHECK (
        regime_type <> 'health_contribution' OR regime_identifier IS NOT NULL
    );

COMMIT;
