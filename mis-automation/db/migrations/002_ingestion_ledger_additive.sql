-- =====================================================================
-- 002  Additive ingestion ledger objects  (REQUIRES APPROVAL - see README "Schema gaps")
-- Adds ONLY new tables; no object from the approved schema v1.1 is altered.
--   drive_file_events : one row per Drive upload event (FILE identity) - separate from
--                       source_files (CONTENT identity, unique by sha256). Lets an identical
--                       upload under another name/Drive id be retained for audit.
--   service_state     : small key/value store (discovery cursor for downtime recovery).
--   daily_batches     : per report-date reconciliation state + notification de-duplication.
-- =====================================================================
BEGIN;
SET LOCAL search_path = mis, public;

CREATE TABLE drive_file_events (
  event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  drive_file_id     text        NOT NULL,
  version_key       text        NOT NULL,        -- Drive md5Checksum, else modifiedTime: new content under same id = new event
  original_filename text        NOT NULL,
  mime_type         text,
  size_bytes        bigint      CHECK (size_bytes >= 0),
  drive_md5         text,
  drive_created_at  timestamptz NOT NULL,        -- Drive upload/creation time (NOT the report date)
  drive_modified_at timestamptz,
  drive_parent_ids  text[]      NOT NULL DEFAULT '{}',   -- Drive folder ids (configuration data)
  portal_id         integer     REFERENCES reporting_portals(portal_id),
  profile_id        integer     REFERENCES mapping_profiles(profile_id),   -- profile matched by filename
  report_date       date,
  period_start      date,
  period_end        date,
  filename_ts       timestamptz,                 -- timestamp in filename, interpreted in the business time zone
  observed_at       timestamptz,                 -- freshness stamp = filename_ts, else report_date 00:00 business tz
  status            text        NOT NULL DEFAULT 'discovered'
                    CHECK (status IN ('discovered','needs_review','claimed','processed','duplicate_content',
                                      'needs_mapping','failed','dead','empty','skipped')),
  review_reason     text,
  source_file_id    bigint      REFERENCES source_files(source_file_id),
  attempt_count     integer     NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  claim_token       uuid,
  claimed_at        timestamptz,
  lease_expires_at  timestamptz,
  last_error        text,
  processed_at      timestamptz,
  discovered_at     timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (drive_file_id, version_key),
  CHECK (status <> 'claimed' OR (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX ix_events_open   ON drive_file_events (status)
  WHERE status NOT IN ('processed','duplicate_content','skipped');
CREATE INDEX ix_events_report ON drive_file_events (report_date, portal_id);
CREATE INDEX ix_events_source ON drive_file_events (source_file_id) WHERE source_file_id IS NOT NULL;
CREATE TRIGGER trg_events_updated BEFORE UPDATE ON drive_file_events
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE service_state (
  state_key  text PRIMARY KEY,
  value      jsonb       NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(value) = 'object')
);

CREATE TABLE daily_batches (
  report_date        date PRIMARY KEY,
  status             text        NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','complete','incomplete')),
  summary            jsonb       NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(summary) = 'object'),
  completed_at       timestamptz,
  last_notified_hash text,
  last_notified_at   timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'complete' OR completed_at IS NOT NULL)
);
CREATE TRIGGER trg_daily_batches_updated BEFORE UPDATE ON daily_batches
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
COMMIT;
