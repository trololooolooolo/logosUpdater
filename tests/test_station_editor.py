"""Manual station editor: add/edit rows, display names, CSV export/import.

Run: python3 -m unittest discover -s tests
"""
import glob
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ford_db  # noqa: E402
from ford_db import EditError  # noqa: E402

SCHEMA = "CREATE TABLE {t} (ECC TEXT, STATION_ID TEXT, RECEIVER_TYPE TEXT, FREQUENCY TEXT, LOGO_FILENAME TEXT)"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.cfg = {"paths": {"db_fm": os.path.join(d, "fm.db"), "db_dab": os.path.join(d, "dab.db")}}
        self.names = os.path.join(d, "names.json")
        for key, t in (("db_fm", "fm_station_logo"), ("db_dab", "dab_station_logo")):
            c = sqlite3.connect(self.cfg["paths"][key])
            c.execute(SCHEMA.format(t=t))
            c.commit()
            c.close()
        self.sql("FM", "INSERT INTO fm_station_logo VALUES ('E0','D312','F','098500','FORD1')")
        self.sql("DAB", "INSERT INTO dab_station_logo VALUES ('E0','D3A0','D','000000','FORD2')")

    def tearDown(self):
        self.tmp.cleanup()

    def sql(self, typ, q, args=()):
        c = sqlite3.connect(self.cfg["paths"]["db_fm" if typ == "FM" else "db_dab"])
        r = c.execute(q, args).fetchall()
        c.commit()
        c.close()
        return r

    def fm(self):
        return self.sql("FM", "SELECT ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME FROM fm_station_logo ORDER BY 2,4")


class TestAddEdit(Base):
    def test_add_fm(self):
        ford_db.add_station(self.cfg, "FM", "e0", "d3a2", "101,3")
        self.assertIn(("E0", "D3A2", "1", "101300", ""), self.fm())

    def test_add_dab(self):
        ford_db.add_station(self.cfg, "DAB", "E0", "D3A1")
        self.assertIn(("E0", "D3A1", "2", "0", ""),
                      self.sql("DAB", "SELECT ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME FROM dab_station_logo"))

    def test_duplicate_refused(self):
        with self.assertRaises(EditError) as e:
            ford_db.add_station(self.cfg, "FM", "E0", "D312", "98.5")      # the Ford row
        self.assertEqual(e.exception.code, "duplicate")
        with self.assertRaises(EditError) as e:
            ford_db.add_station(self.cfg, "DAB", "E0", "D3A0")
        self.assertEqual(e.exception.code, "duplicate")

    def test_invalid_refused(self):
        for args, code in [(("FM", "E0", "D312", "120.0"), "bad_freq"), (("FM", "E0", "XYZ1", "98.5"), "bad_pi"),
                           (("FM", "", "D312", "98.5"), "no_ecc"), (("FM", "E0", "D312", "abc"), "bad_freq")]:
            with self.assertRaises(EditError) as e:
                ford_db.add_station(self.cfg, *args)
            self.assertEqual(e.exception.code, code, args)

    def test_edit_own_row(self):
        rid = ford_db.add_station(self.cfg, "FM", "E0", "D3A2", "98.5")
        ford_db.update_station(self.cfg, "FM", rid, freq_mhz="98.6")
        self.assertIn(("E0", "D3A2", "1", "098600", ""), self.fm())
        self.assertIn(("E0", "D312", "F", "098500", "FORD1"), self.fm(), "other rows unchanged")

    def test_edit_factory_refused(self):
        rid = self.sql("FM", "SELECT rowid FROM fm_station_logo WHERE RECEIVER_TYPE='F'")[0][0]
        with self.assertRaises(EditError) as e:
            ford_db.update_station(self.cfg, "FM", rid, freq_mhz="98.6")
        self.assertEqual(e.exception.code, "factory_row")

    def test_missing_db_not_created(self):
        os.remove(self.cfg["paths"]["db_dab"])
        with self.assertRaises(EditError) as e:
            ford_db.add_station(self.cfg, "DAB", "E0", "D3A1")
        self.assertEqual(e.exception.code, "no_db")
        self.assertFalse(os.path.exists(self.cfg["paths"]["db_dab"]))


class TestNames(Base):
    def test_names_persist_across_processes_and_stay_out_of_dbs(self):
        ford_db.save_names(self.names, {"e0/d3a2": "Radio Test"})
        out = subprocess.check_output(
            [sys.executable, "-c", f"import ford_db; print(ford_db.load_names({self.names!r})['E0/D3A2'])"],
            cwd=ROOT, text=True).strip()
        self.assertEqual(out, "Radio Test")
        for key in ("db_fm", "db_dab"):
            with open(self.cfg["paths"][key], "rb") as f:
                self.assertNotIn(b"Radio Test", f.read())
        ford_db.save_names(self.names, {"E0/D3A2": ""})
        self.assertEqual(ford_db.load_names(self.names), {})


class TestCsv(Base):
    def test_round_trip_is_a_no_op(self):
        ford_db.add_station(self.cfg, "FM", "E0", "D3A2", "101.3", logo="ABC")
        ford_db.save_names(self.names, {"E0/D3A2": "Radio Test"})
        text = ford_db.export_rows(self.cfg, "E0-D", self.names)
        self.assertTrue(text.startswith("type,ecc,pi,frequency,logo,name,receiver_type\n"))
        self.assertIn("FM,E0,D3A2,101.3,ABC,Radio Test,1", text)
        plan = ford_db.import_rows(self.cfg, text, self.names)
        self.assertEqual((len(plan["add"]), len(plan["update"]), plan["names"]), (0, 0, 0))
        self.assertEqual(len(plan["skipped"]), 2, "the two Ford rows are reported, not changed")

    def test_round_trip_keeps_10khz_frequencies(self):
        self.sql("FM", "INSERT INTO fm_station_logo VALUES ('E0','D7E9','F','100770','X')")
        text = ford_db.export_rows(self.cfg, "E0-D", self.names)
        self.assertIn("FM,E0,D7E9,100.77,X,,F", text)
        self.assertEqual(len(ford_db.import_rows(self.cfg, text, self.names)["add"]), 0)

    def test_import_with_errors(self):
        good = "\n".join(f"FM,E0,D4{i:02X},{88 + i}.1,,Station {i}" for i in range(10))
        text = "type,ecc,pi,frequency,logo,name\n" + good + "\nFM,E0,ZZZZ,99.0,,Bad\nFM,E0,QQ,99.1,,Bad\n"
        plan = ford_db.import_rows(self.cfg, text, self.names)
        self.assertEqual(len(plan["add"]), 10)
        self.assertEqual([r["reason"] for r in plan["rejected"]], ["bad_pi", "bad_pi"])
        self.assertEqual(len(self.fm()), 1, "dry run writes nothing")
        with self.assertRaises(EditError):
            ford_db.import_rows(self.cfg, text, self.names, dry_run=False, token="wrong")
        done = ford_db.import_rows(self.cfg, text, self.names, dry_run=False, token=plan["token"])
        self.assertEqual(len(self.fm()), 11)
        self.assertEqual(len(glob.glob(self.cfg["paths"]["db_fm"] + ".bak-*")), 1)
        self.assertEqual(len(glob.glob(self.cfg["paths"]["db_dab"] + ".bak-*")), 1)
        self.assertEqual(ford_db.load_names(self.names)["E0/D403"], "Station 3")
        self.assertIn("backup", done)

    def test_import_never_touches_factory_rows(self):
        text = "type,ecc,pi,frequency,logo\nFM,E0,D312,98.5,NEWLOGO\n"
        plan = ford_db.import_rows(self.cfg, text, self.names)
        self.assertEqual([s["reason"] for s in plan["skipped"]], ["factory_row"])
        ford_db.import_rows(self.cfg, text, self.names, dry_run=False, token=plan["token"])
        self.assertEqual(self.fm(), [("E0", "D312", "F", "098500", "FORD1")])

    def test_import_updates_logo_of_own_row(self):
        ford_db.add_station(self.cfg, "FM", "E0", "D3A2", "101.3")
        text = "type,ecc,pi,frequency,logo\nFM,E0,D3A2,101.3,NEWLOGO.PNG\n"
        plan = ford_db.import_rows(self.cfg, text, self.names)
        self.assertEqual(len(plan["update"]), 1)
        ford_db.import_rows(self.cfg, text, self.names, dry_run=False, token=plan["token"])
        self.assertIn(("E0", "D3A2", "1", "101300", "NEWLOGO"), self.fm())

    def test_missing_columns(self):
        with self.assertRaises(EditError) as e:
            ford_db.import_rows(self.cfg, "name,frequency\nX,98.5\n", self.names)
        self.assertEqual(e.exception.code, "csv_columns")


if __name__ == "__main__":
    unittest.main()
