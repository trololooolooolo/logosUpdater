"""Data sources -> Ford DBs: country (ECC), validation, output.csv format.

Run: python3 -m unittest discover -s tests
Uses throw-away SQLite files in a temp dir; never touches the working DBs.
"""
import csv
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ford_db  # noqa: E402
from sources.base import DataSource, Station, classify_freq_default  # noqa: E402
from sources.csv_file import CsvFileSource  # noqa: E402

SCHEMA = "CREATE TABLE {t} (ECC TEXT, STATION_ID TEXT, RECEIVER_TYPE TEXT, FREQUENCY TEXT, LOGO_FILENAME TEXT)"


class ListSource(DataSource):
    """In-memory source for tests (not registered: lives outside sources/)."""
    id = "test_list"
    name = "Test"

    def __init__(self, stations, default_ecc=""):
        super().__init__({"default_ecc": default_ecc} if default_ecc else {})
        self._st = stations

    def needs_update(self):
        return True, None

    def fetch_stations(self):
        return list(self._st)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)          # output.csv / last_change.txt are cwd-relative
        self.cfg = {"paths": {"db_fm": "fm.db", "db_dab": "dab.db", "db_ocr": "ocr.db",
                              "logo_dir": "logos"}}
        for f, t in (("fm.db", "fm_station_logo"), ("dab.db", "dab_station_logo")):
            c = sqlite3.connect(f)
            c.execute(SCHEMA.format(t=t))
            c.commit()
            c.close()

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def rows(self, db, table):
        c = sqlite3.connect(db)
        r = c.execute(f"SELECT ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME FROM {table} "
                      "ORDER BY 1,2,4").fetchall()
        c.close()
        return r

    def update(self, source):
        return ford_db.update_database(self.cfg, source)


class TestClassifyAndValidate(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_freq_default("98,5"), (False, "098500"))
        self.assertEqual(classify_freq_default("98.5"), (False, "098500"))
        self.assertEqual(classify_freq_default("100,2 / City *X (h332)*"), (False, "100200"))
        self.assertEqual(classify_freq_default("DAB+"), (True, None))

    def test_validate(self):
        v = ford_db.validate_station
        self.assertIsNone(v("E0", "D312", False, "098500"))
        self.assertIsNone(v("e2", "3a01", True, None))
        self.assertEqual(v("", "D312", False, "098500"), "no country (ECC)")
        self.assertEqual(v("EZ", "D312", False, "098500"), "invalid ECC")
        self.assertEqual(v("E0", "D31", False, "098500"), "invalid PI")
        self.assertEqual(v("E0", "D312", False, "120000"), "invalid frequency")
        self.assertEqual(v("E0", "D312", False, None), "invalid frequency")


class TestSync(Base):
    def test_ecc_from_station(self):
        stats = self.update(ListSource([Station("D312", "Radio Test", "98.5", ecc="E0")]))
        self.assertEqual(self.rows("fm.db", "fm_station_logo"), [("E0", "D312", "1", "098500", "")])
        self.assertEqual(stats["added"], 1)

    def test_default_ecc_dab(self):
        self.update(ListSource([Station("E1A0", "DAB One", "DAB")], default_ecc="E1"))
        self.assertEqual(self.rows("dab.db", "dab_station_logo"), [("E1", "E1A0", "2", "0", "")])

    def test_missing_country_skipped(self):
        stats = self.update(ListSource([Station("D312", "No Country", "98.5")]))
        self.assertEqual(self.rows("fm.db", "fm_station_logo"), [])
        self.assertEqual(stats["skipped"], {"no country (ECC)": 1})

    def test_invalid_rows_counted(self):
        stats = self.update(ListSource([Station("D312", "Too high", "120.0", ecc="E0"),
                                        Station("XYZ", "Bad PI", "98.5", ecc="E0"),
                                        Station("D313", "Ok", "99,1", ecc="E0")]))
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["skipped"], {"invalid frequency": 1, "invalid PI": 1})

    def test_rerun_adds_nothing(self):
        src = ListSource([Station("D312", "FM", "98.5"), Station("D3A0", "DAB", "DAB")], default_ecc="E0")
        self.update(src)
        os.remove("output.csv")                     # force a fresh fetch on the second run
        stats = self.update(src)
        self.assertEqual(stats["added"], 0, "a second update must not duplicate FM or DAB rows")
        self.assertEqual(len(self.rows("dab.db", "dab_station_logo")), 1)

    def test_factory_dab_row_not_duplicated(self):
        c = sqlite3.connect("dab.db")
        c.execute("INSERT INTO dab_station_logo VALUES ('E2','3A01','D','000000','LOGO1')")
        c.commit()
        c.close()
        stats = self.update(ListSource([Station("3A01", "DAB", "DAB")], default_ecc="E2"))
        self.assertEqual(stats["added"], 0)

    def test_same_pi_other_country(self):
        c = sqlite3.connect("fm.db")
        c.execute("INSERT INTO fm_station_logo VALUES ('E2','3201','F','098500','PL')")
        c.commit()
        c.close()
        self.update(ListSource([Station("3201", "Other", "98.5", ecc="E3")]))
        self.assertEqual(self.rows("fm.db", "fm_station_logo"),
                         [("E2", "3201", "F", "098500", "PL"), ("E3", "3201", "1", "098500", "")])


class TestOutputCsv(Base):
    def test_new_format_written_with_ecc(self):
        self.update(ListSource([Station("D312", "R", "98.5", ecc="E0", logo_url="http://x/l.png")]))
        with open("output.csv", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][-1], "ECC")
        self.assertEqual(rows[1][-1], "E0")

    def test_old_format_uses_default_ecc(self):
        with open("output.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ID", "Name", "Freq", "Link", "Logo"])
            w.writerow(["3101", "Radio", "98,5", "", "http://x/l.png"])
        st = ford_db.refresh_station_data(self.cfg, ListSource([], default_ecc="E2"), allow_fetch=False)
        self.assertEqual(st[0]["ecc"], "E2")


class TestCsvSource(Base):
    def test_csv_ecc_column_and_default(self):
        with open("st.csv", "w", encoding="utf-8", newline="") as f:
            f.write('ID,Name,Freq,Link,Logo,ECC\nD312,A,98.5,,,E0\nD313,B,"99,1",,,\nE1A0,C,DAB,,,\n')
        src = CsvFileSource({"path": "st.csv", "default_ecc": "e1"})
        stats = self.update(src)
        self.assertEqual(stats["added"], 3)
        self.assertEqual(self.rows("fm.db", "fm_station_logo"),
                         [("E0", "D312", "1", "098500", ""), ("E1", "D313", "1", "099100", "")])
        self.assertEqual(self.rows("dab.db", "dab_station_logo"), [("E1", "E1A0", "2", "0", "")])


if __name__ == "__main__":
    unittest.main()
