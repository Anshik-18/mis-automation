"""Measures parse / load timings on SYNTHETIC data (no customer data). Needs the test PostgreSQL (MIS_TEST_PG).
Usage: python tools/benchmark.py [rows]"""
from __future__ import annotations
import sys, time
sys.path.insert(0, ".")
from processor import file_parser
from processor.tests import fixtures as fx
from processor.tests.pgharness import PgCase


class Bench(PgCase):
    ACTIVATE = ("kreditbuddha_leads_daily",)

    def runTest(self):
        n = int(BENCH_ROWS)
        H = fx.STD21_HEADERS
        rows = [fx.std21_row(i) for i in range(1, n + 1)]
        data = fx.make_csv(H, rows)
        t = time.perf_counter(); file_parser.read_table(data); parse = time.perf_counter() - t
        name = "kreditbuddha_CLICKPE_leads_20261019_060000.csv"
        t = time.perf_counter(); r1 = self.ingest(name, data); first = time.perf_counter() - t
        data2 = fx.make_csv(H, rows + [fx.std21_row(0)])      # same rows, plus one: content differs, 99.99% unchanged
        t = time.perf_counter(); r2 = self.ingest("kreditbuddha_CLICKPE_leads_20261019_070000.csv", data2, created="2026-10-19T15:00:00+00:00"); rep = time.perf_counter() - t
        upd = [fx.std21_row(i, status="DISBURSED", **{"Disbursment Amount": "1000", "Disbursment Date": "2026-10-19"}) if i % 20 == 0 else rows[i - 1] for i in range(1, n + 1)]
        t = time.perf_counter(); r3 = self.ingest("kreditbuddha_CLICKPE_leads_20261019_080000.csv", fx.make_csv(H, upd), created="2026-10-19T16:00:00+00:00"); part = time.perf_counter() - t
        print(f"rows={n}  parse={parse:.2f}s  initial_load={first:.2f}s  repeat_(+1 row)={rep:.2f}s  partial_update_(5% changed)={part:.2f}s")
        for label, r in (("initial", r1), ("repeat", r2), ("partial", r3)):
            print(label, r.get("outcome"), r.get("rows"))


BENCH_ROWS = sys.argv[1] if len(sys.argv) > 1 else "20000"
if __name__ == "__main__":
    import unittest
    unittest.TextTestRunner(verbosity=0).run(unittest.TestSuite([Bench()]))
