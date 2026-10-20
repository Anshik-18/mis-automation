-- =====================================================================
-- 004  Allow a mapping proposal to be REJECTED  (REQUIRES APPROVAL - additive change to schema v1.1)
-- Today a proposal can only be approved or left pending, so a wrong proposal sits in 'proposed' forever and can be
-- picked up again. This adds a terminal 'rejected' status with who/when/why. Nothing is deleted or rewritten:
--   * the status CHECK gains one value ('rejected'); existing rows are unaffected
--   * three nullable columns record the decision
--   * a rejected profile must name who rejected it
-- Rejected profiles are ignored by profile selection, by "reuse the pending draft", and cannot be approved.
-- =====================================================================
BEGIN;
ALTER TABLE mis.mapping_profiles DROP CONSTRAINT IF EXISTS mapping_profiles_status_check;
ALTER TABLE mis.mapping_profiles ADD CONSTRAINT mapping_profiles_status_check
  CHECK (status IN ('draft','proposed','approved','active','retired','rejected'));
ALTER TABLE mis.mapping_profiles
  ADD COLUMN IF NOT EXISTS rejected_by     text,
  ADD COLUMN IF NOT EXISTS rejected_at     timestamptz,
  ADD COLUMN IF NOT EXISTS rejected_reason text;
ALTER TABLE mis.mapping_profiles ADD CONSTRAINT mapping_profiles_rejected_named
  CHECK (status <> 'rejected' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL));
COMMIT;
