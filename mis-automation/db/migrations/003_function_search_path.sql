-- =====================================================================
-- 003  Pin search_path on the schema's trigger functions  (REQUIRES APPROVAL - correctness fix to v1.1)
-- Finding: v1.1 validation ran with `SET search_path = mis, public`. The trigger functions reference
-- tables unqualified, so any client connected WITHOUT that search_path (psql, BI tools, another app) fails
-- on the first write with  relation "mapping_profiles" / "leads" does not exist.
-- Fix: bind the search_path to the functions themselves. No table, column, constraint or logic changes.
-- The processor additionally sets search_path on every connection, so it works either way.
-- =====================================================================
BEGIN;
ALTER FUNCTION mis.set_updated_at()                 SET search_path = mis, public;
ALTER FUNCTION mis.guard_profile_frozen()           SET search_path = mis, public;
ALTER FUNCTION mis.guard_profile_children_frozen()  SET search_path = mis, public;
ALTER FUNCTION mis.leads_merge_guard()              SET search_path = mis, public;
ALTER FUNCTION mis.record_lead_history()            SET search_path = mis, public;
COMMIT;
