-- =====================================================================
-- MIS lead-ingestion schema  v1  (PROPOSED - pending review/approval)
-- Target: PostgreSQL 14+ (tested on 16).  Single schema: mis
-- Scope : data model only. No importer / workflow / API code.
-- =====================================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS mis;
SET LOCAL search_path = mis, public;

-- ---------------------------------------------------------------------
-- 0. Shared helpers
-- ---------------------------------------------------------------------
CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------
-- 1. Reporting portals  (WHERE a report was downloaded from)
-- ---------------------------------------------------------------------
CREATE TABLE reporting_portals (
  portal_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  portal_code   text        NOT NULL UNIQUE
                CHECK (portal_code ~ '^[a-z0-9_]+$'),          -- e.g. credme, kreditbuddha
  display_name  text        NOT NULL,
  website_url   text,
  is_active     boolean     NOT NULL DEFAULT true,
  notes         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_reporting_portals_updated BEFORE UPDATE ON reporting_portals
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- 2. Attribution partners  (WHO generated the lead; independent of portal)
-- ---------------------------------------------------------------------
CREATE TABLE attribution_partners (
  partner_id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attribution_code  text        NOT NULL UNIQUE
                    CHECK (attribution_code ~ '^[0-9A-Z]{3}$'),  -- text: keeps leading zeros, upper-case only
  partner_name      text        NOT NULL,
  is_active         boolean     NOT NULL DEFAULT true,
  notes             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (partner_id, attribution_code)                          -- target of leads' composite FK
);
CREATE TRIGGER trg_attribution_partners_updated BEFORE UPDATE ON attribution_partners
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- 3. Reference: normalized lead statuses  (extend by INSERT, not by ALTER)
-- ---------------------------------------------------------------------
CREATE TABLE lead_statuses (
  status_code           text     PRIMARY KEY CHECK (status_code ~ '^[a-z_]+$'),
  display_name          text     NOT NULL,
  stage_order           smallint NOT NULL,       -- funnel position, for display/sorting only
  is_terminal           boolean  NOT NULL,
  implies_disbursement  boolean  NOT NULL DEFAULT false,  -- used by profile rule "only_if_status_implies_disbursement"
  description           text
);

-- ---------------------------------------------------------------------
-- 4. Reference: canonical fields a mapping may target
--    (adding a field = add a leads column + one row here; a new PORTAL needs no DDL)
-- ---------------------------------------------------------------------
CREATE TABLE canonical_fields (
  field_name   text PRIMARY KEY,
  data_type    text NOT NULL CHECK (data_type IN ('text','date','numeric','boolean','integer')),
  target       text NOT NULL CHECK (target IN ('identity','lead_column','attribution_evidence')),
  description  text
);

-- ---------------------------------------------------------------------
-- 5. Mapping registry
-- ---------------------------------------------------------------------
CREATE TABLE mapping_profiles (
  profile_id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  portal_id             integer NOT NULL REFERENCES reporting_portals(portal_id),
  profile_key           text    NOT NULL CHECK (profile_key ~ '^[a-z0-9_]+$'),   -- stable logical name
  version               integer NOT NULL CHECK (version > 0),
  report_type           text    NOT NULL CHECK (report_type IN ('lead_export','disbursed_cases')),
  import_mode           text    NOT NULL CHECK (import_mode IN ('snapshot','incremental','enrichment')),
  layout_family         text,                                   -- informational label grouping compatible layouts
  derived_from_profile_id integer REFERENCES mapping_profiles(profile_id),
  -- how to recognise & read the file
  file_format           text    NOT NULL CHECK (file_format IN ('csv','xlsx')),
  sheet_name            text,
  header_row            smallint NOT NULL DEFAULT 1 CHECK (header_row >= 1),
  header_signature      text    NOT NULL CHECK (header_signature ~ '^[0-9a-f]{64}$'),
                        -- sha256 of the normalized (trimmed, lower-cased, sorted) header set
  filename_regex        text    NOT NULL,                       -- named groups: report_date [, report_time, period_start, period_end]
  report_date_format    text    NOT NULL CHECK (report_date_format IN ('YYYYMMDD','YYYY-MM-DD')),
  footer_row_regex      text,                                   -- row with blank identity whose any cell matches => footer, not a lead
  -- behaviour
  on_unknown_lead       text    NOT NULL DEFAULT 'create'
                        CHECK (on_unknown_lead IN ('create','quarantine','skip')),
  implied_status_code   text    REFERENCES lead_statuses(status_code),  -- e.g. 'disbursed' for disbursed-only reports
  -- lifecycle
  status                text    NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','proposed','approved','active','retired')),
  proposed_by           text    NOT NULL DEFAULT 'human' CHECK (proposed_by IN ('human','ai')),
  approved_by           text,
  approved_at           timestamptz,
  activated_at          timestamptz,
  retired_at            timestamptz,
  notes                 text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (portal_id, profile_key, version),
  CHECK (status NOT IN ('approved','active','retired') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK (report_type <> 'disbursed_cases' OR import_mode = 'enrichment')
);
-- at most one active version per logical profile, and header-fingerprint lookup is unambiguous
CREATE UNIQUE INDEX uq_profile_one_active ON mapping_profiles (portal_id, profile_key) WHERE status = 'active';
CREATE UNIQUE INDEX uq_profile_active_signature ON mapping_profiles (portal_id, header_signature) WHERE status = 'active';
CREATE TRIGGER trg_mapping_profiles_updated BEFORE UPDATE ON mapping_profiles
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Approved/active/retired profiles are frozen (only lifecycle columns may change).
CREATE FUNCTION guard_profile_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status IN ('approved','active','retired')
     AND (to_jsonb(NEW) - ARRAY['status','activated_at','retired_at','approved_by','approved_at','notes','updated_at'])
         IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['status','activated_at','retired_at','approved_by','approved_at','notes','updated_at'])
  THEN
    RAISE EXCEPTION 'mapping profile % is % and cannot be edited; create a new version', OLD.profile_id, OLD.status;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_mapping_profiles_frozen BEFORE UPDATE ON mapping_profiles
  FOR EACH ROW EXECUTE FUNCTION guard_profile_frozen();

-- 5a. Source column -> canonical field / extras / excluded
CREATE TABLE mapping_profile_columns (
  column_map_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id      integer NOT NULL REFERENCES mapping_profiles(profile_id) ON DELETE CASCADE,
  source_header   text    NOT NULL,
  header_key      text    GENERATED ALWAYS AS (lower(regexp_replace(btrim(source_header), '\s+', ' ', 'g'))) STORED,
  role            text    NOT NULL CHECK (role IN ('canonical','extra','exclude')),
  canonical_field text    REFERENCES canonical_fields(field_name),
  extra_key       text    CHECK (extra_key ~ '^[a-z0-9_]+$'),           -- key inside leads.source_attributes
  alias_priority  smallint NOT NULL DEFAULT 1,                          -- several headers -> one field: lowest priority number wins if non-blank
  transform       text    NOT NULL DEFAULT 'text'
                  CHECK (transform IN ('text','upper','number','date','yes_no_boolean','redact_digit_runs')),   -- closed allow-list, no code
  date_format     text    CHECK (date_format IN ('YYYY-MM-DD','DD Mon YYYY','YYYY-MM-DD HH24:MI:SS')),
  only_if_status_implies_disbursement boolean NOT NULL DEFAULT false,   -- value counts only on rows whose normalized status implies disbursement; otherwise parked in extra_key
  sensitivity     text    NOT NULL DEFAULT 'none'
                  CHECK (sensitivity IN ('none','contact','personal','identity_document','financial','credential','free_text')),
  llm_sample_policy text  NOT NULL DEFAULT 'none' CHECK (llm_sample_policy IN ('none','redacted','allowed')),
  exclusion_reason text,
  UNIQUE (profile_id, header_key),
  CHECK ((role = 'canonical') = (canonical_field IS NOT NULL)),
  CHECK (role <> 'extra' OR extra_key IS NOT NULL),
  CHECK (role <> 'exclude' OR exclusion_reason IS NOT NULL),
  CHECK ((transform = 'date') = (date_format IS NOT NULL)),
  CHECK (NOT only_if_status_implies_disbursement OR (role = 'canonical' AND extra_key IS NOT NULL)),
  CHECK (sensitivity = 'none' OR llm_sample_policy <> 'allowed')
);
CREATE INDEX ix_profile_columns_profile ON mapping_profile_columns (profile_id);   -- cascade/lookup (unique index leads with profile_id too; kept explicit for clarity)

-- 5b. Source status -> normalized status
CREATE TABLE mapping_profile_status_map (
  status_map_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id        integer NOT NULL REFERENCES mapping_profiles(profile_id) ON DELETE CASCADE,
  source_status     text    NOT NULL,
  source_status_key text    GENERATED ALWAYS AS (lower(btrim(source_status))) STORED,
  status_code       text    NOT NULL REFERENCES lead_statuses(status_code),
  UNIQUE (profile_id, source_status_key)
);

-- 5c. Attribution extraction rules (per profile, ordered fallbacks)
CREATE TABLE mapping_profile_attribution_rules (
  rule_id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id       integer NOT NULL REFERENCES mapping_profiles(profile_id) ON DELETE CASCADE,
  priority         smallint NOT NULL DEFAULT 1,
  source_field     text    NOT NULL CHECK (source_field IN ('utm_source','utm_medium','utm_campaign','utm_term')),
  strip_prefixes   text[]  NOT NULL DEFAULT '{}',        -- case-insensitive literal prefixes removed before matching, e.g. {CLICKPE}
  identifier_regex text    NOT NULL DEFAULT '^([0-9A-Z]{3})[0-9A-F]{12}',
                   -- applied to UPPER(value minus prefix); group 1 = the 3-char code. Non-match => unresolved (never guessed)
  organic_values   text[]  NOT NULL DEFAULT '{}',        -- exact, case-insensitive values meaning "no partner", e.g. {ORGANIC}
  notes            text,
  UNIQUE (profile_id, priority),
  CHECK (identifier_regex LIKE '%(%)%')                  -- must contain a capture group
);

-- 5d. Validation rules (closed allow-list of rule types)
CREATE TABLE mapping_profile_validation_rules (
  rule_id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id       integer NOT NULL REFERENCES mapping_profiles(profile_id) ON DELETE CASCADE,
  canonical_field  text    NOT NULL REFERENCES canonical_fields(field_name),
  rule_type        text    NOT NULL CHECK (rule_type IN ('required','regex','min_value','max_value','allowed_values','not_after_report_date')),
  rule_param       jsonb   NOT NULL DEFAULT '{}',
  on_fail          text    NOT NULL DEFAULT 'quarantine_row' CHECK (on_fail IN ('quarantine_row','warn')),
  UNIQUE (profile_id, canonical_field, rule_type),
  CHECK (jsonb_typeof(rule_param) = 'object')
);

-- Frozen-profile guard for all four child tables
CREATE FUNCTION guard_profile_children_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_pid integer; v_status text;
BEGIN
  IF TG_OP = 'DELETE' THEN v_pid := OLD.profile_id; ELSE v_pid := NEW.profile_id; END IF;
  SELECT status INTO v_status FROM mapping_profiles WHERE profile_id = v_pid;
  IF FOUND AND v_status IN ('approved','active','retired') THEN
    RAISE EXCEPTION 'profile % is % ; its mapping rows are frozen. Create a new version.', v_pid, v_status;
  END IF;
  RETURN COALESCE(NEW, OLD);
END $$;
CREATE TRIGGER trg_cols_frozen   BEFORE INSERT OR UPDATE OR DELETE ON mapping_profile_columns           FOR EACH ROW EXECUTE FUNCTION guard_profile_children_frozen();
CREATE TRIGGER trg_status_frozen BEFORE INSERT OR UPDATE OR DELETE ON mapping_profile_status_map        FOR EACH ROW EXECUTE FUNCTION guard_profile_children_frozen();
CREATE TRIGGER trg_attr_frozen   BEFORE INSERT OR UPDATE OR DELETE ON mapping_profile_attribution_rules FOR EACH ROW EXECUTE FUNCTION guard_profile_children_frozen();
CREATE TRIGGER trg_valid_frozen  BEFORE INSERT OR UPDATE OR DELETE ON mapping_profile_validation_rules   FOR EACH ROW EXECUTE FUNCTION guard_profile_children_frozen();

-- ---------------------------------------------------------------------
-- 6. Source files  (one row per distinct file content; ingestion ledger)
-- ---------------------------------------------------------------------
CREATE TABLE source_files (
  source_file_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  portal_id            integer REFERENCES reporting_portals(portal_id),     -- NULL until portal is determined
  profile_id           integer REFERENCES mapping_profiles(profile_id),     -- mapping version used (NULL until matched)
  original_filename    text    NOT NULL,
  drive_file_id        text,
  content_sha256       text    NOT NULL UNIQUE CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),   -- exact-duplicate guard
  size_bytes           bigint  CHECK (size_bytes >= 0),
  report_date          date,                       -- from filename; NOT lead_entry_date / disbursed_date
  period_start         date,                       -- only for range-style filenames
  period_end           date,
  observed_at          timestamptz,                -- freshness stamp: filename timestamp if present, else report_date at 00:00 (tz = business decision)
  uploaded_at          timestamptz,                -- when file reached Google Drive
  discovered_at        timestamptz NOT NULL DEFAULT now(),
  processing_started_at timestamptz,
  processed_at         timestamptz,                -- set only on success
  processing_status    text    NOT NULL DEFAULT 'discovered'
                       CHECK (processing_status IN ('discovered','needs_mapping','ready','processing','processed','failed','empty','rejected')),
  attempt_count        integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  rows_total           integer CHECK (rows_total >= 0),
  rows_data            integer CHECK (rows_data >= 0),
  rows_footer          integer CHECK (rows_footer >= 0),
  rows_inserted        integer CHECK (rows_inserted >= 0),
  rows_updated         integer CHECK (rows_updated >= 0),
  rows_unchanged       integer CHECK (rows_unchanged >= 0),
  rows_stale_skipped   integer CHECK (rows_stale_skipped >= 0),
  rows_quarantined     integer CHECK (rows_quarantined >= 0),
  footer_totals        jsonb,                      -- parsed footer figures, to reconcile against loaded rows
  error_message        text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (period_end IS NULL OR period_start IS NULL OR period_end >= period_start),
  CHECK (footer_totals IS NULL OR jsonb_typeof(footer_totals) = 'object'),
  CHECK (processing_status <> 'processed'
         OR (processed_at IS NOT NULL AND portal_id IS NOT NULL AND profile_id IS NOT NULL
             AND report_date IS NOT NULL AND observed_at IS NOT NULL))
);
CREATE INDEX ix_source_files_portal_report ON source_files (portal_id, report_date);
CREATE INDEX ix_source_files_drive         ON source_files (drive_file_id) WHERE drive_file_id IS NOT NULL;
CREATE INDEX ix_source_files_open          ON source_files (processing_status) WHERE processing_status NOT IN ('processed','empty');
CREATE TRIGGER trg_source_files_updated BEFORE UPDATE ON source_files
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- 7. Canonical leads  (single table for every portal)
-- ---------------------------------------------------------------------
CREATE TABLE leads (
  lead_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  portal_id            integer NOT NULL REFERENCES reporting_portals(portal_id),
  external_lead_id     text    NOT NULL CHECK (btrim(external_lead_id) <> '' AND external_lead_id = btrim(external_lead_id)),
  -- attribution (code kept even if the partner is not registered yet)
  attribution_status   text    NOT NULL DEFAULT 'unresolved'
                       CHECK (attribution_status IN ('resolved','unregistered_code','organic','unresolved')),
  attribution_code     text    CHECK (attribution_code ~ '^[0-9A-Z]{3}$'),
  attribution_partner_id integer,
  -- lifecycle
  lead_status_code     text    NOT NULL REFERENCES lead_statuses(status_code),
  lead_status_raw      text,
  status_reason        text,                     -- rejection reason / remark (digit runs redacted by transform)
  is_doable            boolean,
  -- dates (never interchangeable)
  lead_entry_date      date,
  disbursed_date       date,
  -- money (INR assumed; see open questions)
  requested_loan_amount numeric(14,2) CHECK (requested_loan_amount >= 0),
  approved_amount      numeric(14,2) CHECK (approved_amount >= 0),
  disbursed_amount     numeric(14,2) CHECK (disbursed_amount >= 0),
  payout_amount        numeric(14,2) CHECK (payout_amount >= 0),
  -- low-sensitivity descriptors
  lead_source          text,
  employment_type      text,
  state                text,
  city                 text,
  pincode              text CHECK (pincode ~ '^[0-9]{6}$'),
  source_attributes    jsonb   NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(source_attributes) = 'object'),
  -- freshness guards (see upsert strategy) and provenance
  status_observed_at       timestamptz NOT NULL,
  disbursement_observed_at timestamptz NOT NULL,
  first_seen_file_id   bigint  NOT NULL REFERENCES source_files(source_file_id),
  last_seen_file_id    bigint  NOT NULL REFERENCES source_files(source_file_id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_leads_identity UNIQUE (portal_id, external_lead_id),
  CONSTRAINT fk_leads_partner_code FOREIGN KEY (attribution_partner_id, attribution_code)
             REFERENCES attribution_partners (partner_id, attribution_code),
  CONSTRAINT ck_leads_attribution CHECK (
       (attribution_status = 'resolved'          AND attribution_partner_id IS NOT NULL AND attribution_code IS NOT NULL)
    OR (attribution_status = 'unregistered_code' AND attribution_partner_id IS NULL     AND attribution_code IS NOT NULL)
    OR (attribution_status IN ('organic','unresolved') AND attribution_partner_id IS NULL AND attribution_code IS NULL))
);
-- partner leads by entry date (counts, monthly series, partner comparison)
CREATE INDEX ix_leads_partner_entry ON leads (attribution_partner_id, lead_entry_date);
-- partner disbursement totals by disbursement date; covering for index-only SUM/COUNT
CREATE INDEX ix_leads_partner_disb  ON leads (attribution_partner_id, disbursed_date)
  INCLUDE (portal_id, disbursed_amount) WHERE disbursed_amount IS NOT NULL;
-- date-range / portal dashboards without a partner filter
CREATE INDEX ix_leads_entry_portal  ON leads (lead_entry_date, portal_id);
CREATE TRIGGER trg_leads_updated BEFORE UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 7a. Attribution evidence (raw UTMs; restricted - may embed customer names in suffixes)
CREATE TABLE lead_attribution_evidence (
  lead_id         bigint PRIMARY KEY REFERENCES leads(lead_id) ON DELETE CASCADE,
  source_file_id  bigint NOT NULL REFERENCES source_files(source_file_id),
  profile_id      integer NOT NULL REFERENCES mapping_profiles(profile_id),         -- mapping version used
  rule_id         integer REFERENCES mapping_profile_attribution_rules(rule_id),    -- NULL when no rule matched
  utm_source      text,
  utm_medium      text,
  utm_campaign    text,
  utm_term        text,
  created_at      timestamptz NOT NULL DEFAULT now()
);
-- (profile_id/source_file_id FK indexes intentionally omitted: parents are rarely deleted)

-- ---------------------------------------------------------------------
-- 8. Lead history: append-only, written by trigger only when tracked fields change
-- ---------------------------------------------------------------------
CREATE TABLE lead_history (
  history_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id           bigint  NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
  source_file_id    bigint  NOT NULL REFERENCES source_files(source_file_id),
  observed_at       timestamptz NOT NULL,
  lead_status_code  text    NOT NULL,
  lead_status_raw   text,
  approved_amount   numeric(14,2),
  disbursed_amount  numeric(14,2),
  disbursed_date    date,
  payout_amount     numeric(14,2),
  recorded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_lead_history_lead ON lead_history (lead_id, history_id DESC);

CREATE FUNCTION record_lead_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT'
     OR (NEW.lead_status_code, NEW.lead_status_raw, NEW.approved_amount, NEW.disbursed_amount, NEW.disbursed_date, NEW.payout_amount)
        IS DISTINCT FROM
        (OLD.lead_status_code, OLD.lead_status_raw, OLD.approved_amount, OLD.disbursed_amount, OLD.disbursed_date, OLD.payout_amount)
  THEN
    INSERT INTO lead_history (lead_id, source_file_id, observed_at, lead_status_code, lead_status_raw,
                              approved_amount, disbursed_amount, disbursed_date, payout_amount)
    VALUES (NEW.lead_id, NEW.last_seen_file_id, GREATEST(NEW.status_observed_at, NEW.disbursement_observed_at),
            NEW.lead_status_code, NEW.lead_status_raw,
            NEW.approved_amount, NEW.disbursed_amount, NEW.disbursed_date, NEW.payout_amount);
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_leads_history AFTER INSERT OR UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION record_lead_history();

-- ---------------------------------------------------------------------
-- 9. Ingestion issues / quarantine  (rows that could not be safely applied)
--    details must hold ONLY non-sensitive fields (no names, phone, DOB, documents)
-- ---------------------------------------------------------------------
CREATE TABLE ingestion_issues (
  issue_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_file_id    bigint  NOT NULL REFERENCES source_files(source_file_id),
  row_number        integer,
  issue_type        text    NOT NULL CHECK (issue_type IN (
                      'missing_external_id','duplicate_in_file','validation_failed','unmapped_status',
                      'attribution_conflict','unknown_lead_skipped','stale_update_skipped','footer_mismatch',
                      'absent_from_snapshot','other')),
  severity          text    NOT NULL DEFAULT 'warning' CHECK (severity IN ('error','warning','info')),
  external_lead_id  text,
  lead_id           bigint  REFERENCES leads(lead_id) ON DELETE SET NULL,
  message           text,
  details           jsonb   NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details) = 'object'),
  is_resolved       boolean NOT NULL DEFAULT false,
  resolved_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_issues_file ON ingestion_issues (source_file_id);
CREATE INDEX ix_issues_open ON ingestion_issues (issue_type) WHERE NOT is_resolved;

-- ---------------------------------------------------------------------
-- 10. Reference seed data (needed by FKs; proposed, subject to approval)
-- ---------------------------------------------------------------------
INSERT INTO lead_statuses (status_code, display_name, stage_order, is_terminal, implies_disbursement, description) VALUES
 ('registered',           'Registered',            10, false, false, 'Lead created / registered, no application yet'),
 ('partial',              'Partial application',   20, false, false, 'Application started but incomplete'),
 ('in_process',           'In process',            30, false, false, 'Application under processing'),
 ('on_hold',              'On hold / sent back',   35, false, false, 'Held or sent back for correction'),
 ('approved',             'Approved / sanctioned', 40, false, false, 'Approved or sanctioned, not yet disbursed'),
 ('disbursement_pending', 'Disbursement pending',  50, false, false, 'Disbursal initiated but not completed'),
 ('disbursed',            'Disbursed',             60, false, true,  'Loan disbursed'),
 ('closed',               'Closed',                70, true,  false, 'Closed after disbursal (meaning to be confirmed)'),
 ('withdrawn',            'Withdrawn',             80, true,  false, 'Withdrawn / auto-withdrawn'),
 ('rejected',             'Rejected',              90, true,  false, 'Rejected (system or manual)'),
 ('unmapped',             'Unmapped status',       99, false, false, 'Source status has no approved mapping - needs review');

INSERT INTO canonical_fields (field_name, data_type, target, description) VALUES
 ('external_lead_id',      'text',    'identity',             'Portal-assigned lead identifier (kept as text)'),
 ('lead_status_raw',       'text',    'lead_column',          'Status exactly as reported by the portal'),
 ('status_reason',         'text',    'lead_column',          'Rejection reason or remark'),
 ('is_doable',             'boolean', 'lead_column',          'Portal "Doable" eligibility flag'),
 ('lead_entry_date',       'date',    'lead_column',          'Date lead entered the portal system'),
 ('disbursed_date',        'date',    'lead_column',          'Date of disbursement'),
 ('requested_loan_amount', 'numeric', 'lead_column',          'Amount requested by customer'),
 ('approved_amount',       'numeric', 'lead_column',          'Approved / sanctioned amount'),
 ('disbursed_amount',      'numeric', 'lead_column',          'Amount actually disbursed'),
 ('payout_amount',         'numeric', 'lead_column',          'Payout reported by portal'),
 ('lead_source',           'text',    'lead_column',          'Portal lead-source / channel label'),
 ('employment_type',       'text',    'lead_column',          'Salaried / self-employed'),
 ('state',                 'text',    'lead_column',          'State'),
 ('city',                  'text',    'lead_column',          'City'),
 ('pincode',               'text',    'lead_column',          'Postal code (text)'),
 ('utm_source',            'text',    'attribution_evidence', 'Raw UTM source'),
 ('utm_medium',            'text',    'attribution_evidence', 'Raw UTM medium'),
 ('utm_campaign',          'text',    'attribution_evidence', 'Raw UTM campaign'),
 ('utm_term',              'text',    'attribution_evidence', 'Raw UTM term');

COMMIT;
