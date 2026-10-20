"""PostgreSQL test harness: one fresh database per test class, migrations + seeds applied, profiles activated."""
from __future__ import annotations

import hashlib
import itertools
import os
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from processor import events, ingest, migrate, seed
from processor.config import Settings
from processor.database import Database, connect
from processor.drive_metadata import DriveFile
from processor.guards import required_acceptances
from processor.schema_registry import approve_and_activate, load_spec_from_db
from processor.tests import fixtures as fx

BASE = os.environ.get("MIS_TEST_PG", "host=/var/lib/postgresql port=5544 user=postgres")


def pg_available() -> bool:
    try:
        psycopg.connect(BASE + " dbname=postgres", connect_timeout=3).close()
        return True
    except Exception:
        return False


_ids = itertools.count(1)


class PgCase(unittest.TestCase):
    ACTIVATE = "all"                       # 'all' | iterable of profile_keys
    ISOLATED = False                       # True: every test gets its own copy of the migrated+seeded database
    SETTINGS: dict = {}

    @classmethod
    def setUpClass(cls):
        if not pg_available():
            raise unittest.SkipTest("PostgreSQL not available (set MIS_TEST_PG)")
        cls.dbname = "mis_t_" + uuid.uuid4().hex[:10]
        admin = psycopg.connect(BASE + " dbname=postgres", autocommit=True)
        admin.execute(f"CREATE DATABASE {cls.dbname}")
        admin.close()
        cls.url = f"{BASE} dbname={cls.dbname}"
        migrate.run(cls.url, approved=tuple(x for x in os.environ.get("MIS_TEST_APPROVED", "002,003").split(",") if x))
        c = connect(cls.url)
        seed.seed(c, fx.SEED_DIR)
        keys = None if cls.ACTIVATE == "all" else set(cls.ACTIVATE)
        for pid, key in c.execute("SELECT profile_id, profile_key FROM mis.mapping_profiles ORDER BY profile_id").fetchall():
            if keys is None or key in keys:
                approve_and_activate(c, pid, "test-approver", sorted(required_acceptances(load_spec_from_db(c, pid))))
        c.commit()
        c.close()
        cls.settings = Settings(database_url=cls.url, max_attempts=3, **cls.SETTINGS)
        cls.db = Database(cls.url, use_pool=False)

    @classmethod
    def tearDownClass(cls):
        try:
            admin = psycopg.connect(BASE + " dbname=postgres", autocommit=True)
            admin.execute(f"DROP DATABASE IF EXISTS {cls.dbname} WITH (FORCE)")
            admin.close()
        except Exception:
            pass

    def _fresh(self):
        admin = psycopg.connect(BASE + " dbname=postgres", autocommit=True)
        self._copy = "mis_c_" + uuid.uuid4().hex[:10]
        admin.execute(f"CREATE DATABASE {self._copy} TEMPLATE {self.dbname}")
        admin.close()
        self._orig = (self.url, self.db, self.settings)
        self.url = f"{BASE} dbname={self._copy}"
        self.db = Database(self.url, use_pool=False)
        from dataclasses import replace
        self.settings = replace(self.settings, database_url=self.url)

    def _drop_fresh(self):
        self.url, self.db, self.settings = self._orig
        admin = psycopg.connect(BASE + " dbname=postgres", autocommit=True)
        admin.execute(f"DROP DATABASE IF EXISTS {self._copy} WITH (FORCE)")
        admin.close()

    def run(self, result=None):
        if self.ISOLATED:
            self._fresh()
            try:
                return super().run(result)
            finally:
                self._drop_fresh()
        return super().run(result)

    # ---- helpers -------------------------------------------------------------------------------
    def q(self, sql, params=()):
        with connect(self.url) as c:
            return c.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        with connect(self.url) as c:
            c.execute(sql, params)
            c.commit()

    def reset_data(self):
        with connect(self.url) as c:
            c.execute("TRUNCATE mis.lead_history, mis.lead_attribution_evidence, mis.ingestion_issues, mis.leads, "
                      "mis.drive_file_events, mis.source_files, mis.daily_batches, mis.service_state RESTART IDENTITY CASCADE")
            c.execute("DELETE FROM mis.attribution_partners")
            c.commit()

    def drive_file(self, name, data, *, created="2026-10-19T14:00:00+00:00", file_id=None, parents=("folderA",)):
        return DriveFile(file_id=file_id or f"drv{next(_ids)}", name=name, mime_type="text/csv",
                         created_at=datetime.fromisoformat(created), modified_at=datetime.fromisoformat(created),
                         size_bytes=len(data), md5=hashlib.md5(data).hexdigest(), parents=parents)

    def ingest(self, name, data, **kw):
        """register -> claim -> process. Returns the processor result dict (or the registration if not claimable)."""
        f = self.drive_file(name, data, **kw)
        with self.db.connection() as c:
            reg = events.register(c, f, tz=self.settings.tz, range_policy=self.settings.range_end_future_policy)
            cl = events.claim(c, reg["event_id"], lease_minutes=30, max_attempts=self.settings.max_attempts) \
                if reg["status"] == "discovered" else None
        if not cl:
            return {"outcome": "not_claimed", **reg}
        out = ingest.process_event(self.db, self.settings, cl["event_id"], cl["claim_token"], data)
        out["_reg"] = reg
        return out

    def lead(self, portal, ext, ns="lead_id"):
        r = self.q("""SELECT l.lead_status_code, l.lead_status_raw, l.disbursed_amount, l.approved_amount, l.attribution_status,
                             l.attribution_code, l.state, l.requested_loan_amount, l.disbursed_date, l.payout_amount,
                             l.lead_entry_date, l.source_attributes
                      FROM mis.leads l JOIN mis.reporting_portals p USING (portal_id)
                      WHERE p.portal_code=%s AND l.external_lead_id=%s AND l.id_namespace=%s""", (portal, ext, ns))
        if not r:
            return None
        keys = ("status", "raw", "disbursed", "approved", "attr", "code", "state", "requested", "disb_date", "payout",
                "entry", "attrs")
        return dict(zip(keys, r[0]))
