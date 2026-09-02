from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
import json
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from app import create_app, startup_database_path
from physio_core import (
    migrate_future_appointments, migrate_receipt_amount, migrate_session_coverage,
)


SCHEMA = """
CREATE TABLE referrals (
    referral_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT,
    address TEXT, work_phone TEXT, mobile_phone TEXT, notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE professions (
    profession_id INTEGER PRIMARY KEY AUTOINCREMENT, profession_name TEXT NOT NULL,
    profession_category TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE doctors (
    doctor_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, specialty TEXT,
    work_phone TEXT, home_phone TEXT, mobile_phone TEXT, email TEXT, notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE patients (
    patient_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, gender TEXT,
    mobile_phone TEXT, home_phone TEXT, work_phone TEXT, email TEXT, birthdate TEXT,
    identity_number TEXT, address TEXT, city TEXT, postal_code TEXT,
    referral_id INTEGER, profession_id INTEGER, notes TEXT, photo_path TEXT,
    is_active INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (referral_id) REFERENCES referrals(referral_id),
    FOREIGN KEY (profession_id) REFERENCES professions(profession_id)
);
CREATE TABLE clinical_histories (
    history_id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, history_date TEXT,
    problem_description TEXT, main_diagnosis TEXT, date_completed TEXT,
    is_active INTEGER DEFAULT 0, doctor_id INTEGER, social_security TEXT, body_area TEXT,
    for_print INTEGER DEFAULT 0, for_xrays TEXT, for_exercise TEXT, today INTEGER DEFAULT 0,
    icd10_code TEXT, gesy_referral TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id),
    FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
);
CREATE TABLE appointments (
    appointment_id INTEGER PRIMARY KEY, history_id INTEGER NOT NULL,
    appointment_number INTEGER, appointment_date TEXT, appointment_time TEXT,
    status TEXT DEFAULT 'completed', notes TEXT, today INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (history_id) REFERENCES clinical_histories(history_id)
);
CREATE TABLE payments (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT, appointment_id INTEGER NOT NULL,
    payment_date TEXT, amount_due REAL DEFAULT 0, amount_paid REAL DEFAULT 0,
    receipt_amount REAL DEFAULT 0, payment_method TEXT, notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
);
"""


def create_sample_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO referrals(referral_id,first_name,last_name) VALUES(1,'ΙΩΑΝΝΗΣ','ΙΑΤΡΟΣ')")
    con.execute("INSERT INTO professions(profession_name) VALUES('Εκπαιδευτικός')")
    con.execute("INSERT INTO doctors(doctor_id,first_name,last_name) VALUES(1,'ΜΑΡΙΑ','ΙΑΤΡΟΥ')")
    con.execute("INSERT INTO patients(patient_id,first_name,last_name,mobile_phone,is_active) VALUES(1,'ΆΛΕΞ','ΖΗΝΩΝ','99111111',1)")
    con.execute("INSERT INTO patients(patient_id,first_name,last_name,mobile_phone,is_active) VALUES(2,'ΜΑΡΙΑ','ΑΝΔΡΕΟΥ','99222222',1)")
    con.execute("INSERT INTO clinical_histories(history_id,patient_id,history_date,main_diagnosis,is_active,today) VALUES(1,1,'2026-08-20','Ώμος',1,1)")
    con.execute("INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) VALUES(1,1,1,'2026-08-21')")
    con.execute("INSERT INTO payments(appointment_id,payment_date,amount_due,amount_paid,receipt_amount) VALUES(1,'2026-08-21',35,0,0)")
    con.commit()
    con.close()


class PhysioAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "clinic.db"
        self.meta_path = self.root / "meta.db"
        self.backup_dir = self.root / "backups"
        self.selection_path = self.root / "database-selection.json"
        create_sample_db(self.db_path)
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": str(self.db_path),
            "META_DB_PATH": str(self.meta_path),
            "BACKUP_DIR": str(self.backup_dir),
            "DATABASE_SELECTION_PATH": str(self.selection_path),
            "AUTO_BACKUP": False,
        })
        self.client = self.app.test_client()
        self.client.get("/")
        con = sqlite3.connect(self.db_path)
        self.self_standard_plan = con.execute(
            "SELECT coverage_plan_id FROM CoveragePlans WHERE code='SELF_STANDARD'"
        ).fetchone()[0]
        self.gesy_plan = con.execute(
            "SELECT coverage_plan_id FROM CoveragePlans WHERE code='GESY'"
        ).fetchone()[0]
        con.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def csrf(self, client=None) -> str:
        client = client or self.client
        client.get("/")
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def api_headers(self, client=None) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf(client)}

    def count(self, table: str, path: Path | None = None) -> int:
        con = sqlite3.connect(path or self.db_path)
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()

    def future_appointment(self, **overrides):
        payload = {
            "patient_id": 1,
            "history_id": 1,
            "appointment_date": "2026-09-07",
            "start_time": "08:00",
            "duration_minutes": 60,
            "notes": "Δοκιμαστικό ραντεβού",
            "coverage_plan_id": self.self_standard_plan,
        }
        payload.update(overrides)
        return self.client.post(
            "/api/future-appointments", json=payload, headers=self.api_headers(),
        )

    def new_session(self, history_id: int, **overrides):
        payload = {"coverage_plan_id": self.self_standard_plan}
        payload.update(overrides)
        return self.client.post(
            f"/api/appointment/new/{history_id}",
            json=payload, headers=self.api_headers(),
        )

    def create_gesy_referral(self, history_id: int = 1, number: str = "99887766", visits: int = 6):
        response = self.client.post(
            f"/api/histories/{history_id}/gesy-referrals",
            json={"referral_number": number, "allowed_visits": visits},
            headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["gesy_referral_id"]

    def test_get_new_forms_do_not_insert(self):
        patients_before = self.count("patients")
        histories_before = self.count("clinical_histories")
        self.assertEqual(self.client.get("/patients/new").status_code, 200)
        self.assertEqual(self.client.get("/histories/new?patient_id=1").status_code, 200)
        self.assertEqual(self.count("patients"), patients_before)
        self.assertEqual(self.count("clinical_histories"), histories_before)

    def test_explicit_patient_save_and_undo(self):
        before = self.count("patients")
        response = self.client.post("/patients/new", data={
            "csrf_token": self.csrf(), "first_name": "ΝΙΚΟΣ", "last_name": "ΔΟΚΙΜΗ",
            "is_active": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.count("patients"), before + 1)
        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.count("patients"), before)

    def test_patient_delete_cascades_histories_appointments_and_payments_and_undoes(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,main_diagnosis,is_active,today) "
            "VALUES(2,2,'2026-08-22','Δοκιμή διαγραφής',1,1)"
        )
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) "
            "VALUES(2,2,1,'2026-08-23')"
        )
        con.execute(
            "INSERT INTO payments(payment_id,appointment_id,payment_date,amount_due,amount_paid,receipt_amount) "
            "VALUES(2,2,'2026-08-23',35,10,2)"
        )
        con.execute(
            "INSERT INTO Future_appointments(patient_id,history_id,appointment_date,"
            "start_time,duration_minutes,completed_appointment_id) "
            "VALUES(2,2,'2026-08-24','09:00',60,2)"
        )
        con.commit()
        con.close()

        patient_html = self.client.get("/patients/2").get_data(as_text=True)
        self.assertIn('class="danger-btn delete-patient" data-patient="2"', patient_html)
        script = (Path(self.app.static_folder) / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            "Θα διαγραφεί ο ασθενής, τα ιστορικά του και οι παρουσίες του",
            script,
        )

        response = self.client.post(
            "/api/patients/2/delete", json={}, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {key: response.get_json()[key] for key in ("histories", "appointments", "future_appointments", "payments")},
            {"histories": 1, "appointments": 1, "future_appointments": 1, "payments": 1},
        )
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM patients WHERE patient_id=2").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM clinical_histories WHERE patient_id=2").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE history_id=2").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM Future_appointments WHERE patient_id=2").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM payments WHERE appointment_id=2").fetchone()[0], 0)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()
        self.assertTrue(any(self.backup_dir.glob("clinic_*.db")))

        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM patients WHERE patient_id=2").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM clinical_histories WHERE patient_id=2").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE history_id=2").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM Future_appointments WHERE patient_id=2").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM payments WHERE appointment_id=2").fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()

    def test_history_delete_cascades_sessions_and_payments_and_undoes(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) "
            "VALUES(2,1,2,'2026-08-22')"
        )
        con.execute(
            "INSERT INTO payments(payment_id,appointment_id,payment_date,amount_due,amount_paid,receipt_amount) "
            "VALUES(2,2,'2026-08-22',35,10,2)"
        )
        con.execute(
            "INSERT INTO Future_appointments(patient_id,history_id,appointment_date,"
            "start_time,duration_minutes,completed_appointment_id) "
            "VALUES(1,1,'2026-08-24','10:00',45,2)"
        )
        con.commit()
        con.close()

        history_html = self.client.get("/histories/1").get_data(as_text=True)
        self.assertIn('id="delete-history" class="danger-btn" type="button" data-history="1" data-patient="1"', history_html)
        script = (Path(self.app.static_folder) / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Θα διαγραφεί το ιστορικό μαζί με όλες τις συνεδρίες", script)

        response = self.client.post("/api/histories/1/delete", json={}, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {key: response.get_json()[key] for key in ("appointments", "future_appointments", "payments")},
            {"appointments": 2, "future_appointments": 1, "payments": 2},
        )
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM patients WHERE patient_id=1").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM clinical_histories WHERE history_id=1").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE history_id=1").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM Future_appointments WHERE history_id=1").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM payments WHERE appointment_id IN (1,2)").fetchone()[0], 0)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()

        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.count("clinical_histories"), 1)
        self.assertEqual(self.count("appointments"), 2)
        self.assertEqual(self.count("Future_appointments"), 1)
        self.assertEqual(self.count("payments"), 2)

    def test_explicit_history_save_and_new_appointment(self):
        histories_before = self.count("clinical_histories")
        response = self.client.post("/histories/new", data={
            "csrf_token": self.csrf(), "patient_id": "1",
            "history_date": "2026-08-26", "main_diagnosis": "Έλεγχος",
            "is_active": "1", "today": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.count("clinical_histories"), histories_before + 1)
        history_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        appointment = self.new_session(history_id)
        self.assertEqual(appointment.status_code, 200)
        body = appointment.get_json()
        self.assertGreater(body["appointment_id"], 0)
        self.assertGreater(body["payment_id"], 0)
        self.assertEqual(body["receipt_amount"], 0)
        self.assertNotIn("receipt_number", body)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT receipt_amount FROM payments WHERE payment_id=?",
                (body["payment_id"],),
            ).fetchone()[0],
            0.0,
        )
        con.close()

    def test_new_appointment_rejects_a_second_presence_on_the_same_day(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,is_active,today) "
            "VALUES(2,2,'2026-08-22',1,1)"
        )
        con.commit()
        con.close()

        first = self.new_session(2)
        self.assertEqual(first.status_code, 200)
        second = self.new_session(2)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.get_json()["error"],
            "Έχει γίνει ήδη καταχώριση παρουσίας για σήμερα.",
        )
        con = sqlite3.connect(self.db_path)
        count = con.execute(
            "SELECT COUNT(*) FROM appointments WHERE history_id=2 AND appointment_date=?",
            (date.today().isoformat(),),
        ).fetchone()[0]
        con.close()
        self.assertEqual(count, 1)

    def test_appointment_date_update_cannot_create_a_duplicate_presence(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) "
            "VALUES(2,1,2,'2026-08-22')"
        )
        con.commit()
        con.close()

        response = self.client.post("/api/update", json={
            "table": "appointments", "pk": 2,
            "column": "appointment_date", "value": "21/08/2026",
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 409)
        con = sqlite3.connect(self.db_path)
        saved_date = con.execute(
            "SELECT appointment_date FROM appointments WHERE appointment_id=2"
        ).fetchone()[0]
        con.close()
        self.assertEqual(saved_date, "2026-08-22")

    def test_selected_presence_delete_removes_payment_and_undo_restores_both(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO Future_appointments(patient_id,history_id,appointment_date,"
            "start_time,duration_minutes,status,completed_appointment_id) "
            "VALUES(1,1,'2026-08-24','11:00',30,'completed',1)"
        )
        con.commit()
        con.close()
        response = self.client.post(
            "/api/appointments/1/delete", json={}, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.count("payments"), 0)
        con = sqlite3.connect(self.db_path)
        self.assertIsNone(con.execute(
            "SELECT completed_appointment_id FROM Future_appointments"
        ).fetchone()[0])
        con.close()
        self.assertTrue(list(self.backup_dir.glob("*.db")))

        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.count("payments"), 1)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute(
            "SELECT completed_appointment_id FROM Future_appointments"
        ).fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()

    def test_new_non_gesy_appointment_uses_plan_snapshot_not_previous_charge(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE clinical_histories SET social_security='ΓΕΣΥ' WHERE history_id=1")
        con.execute("UPDATE payments SET amount_due=27.5 WHERE appointment_id=1")
        con.commit()
        con.close()

        response = self.new_session(1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["amount_due"], 35.0)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT amount_due FROM payments WHERE payment_id=?",
                (response.get_json()["payment_id"],),
            ).fetchone()[0],
            35.0,
        )
        con.close()

    def test_legacy_social_security_does_not_choose_new_session_coverage(self):
        con = sqlite3.connect(self.db_path)
        con.executemany(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,social_security,is_active,today) VALUES(?,?,?,?,1,0)",
            [
                (2, 2, "2026-08-22", "γεσύ"),
                (3, 2, "2026-08-23", "Ιδιωτική ασφάλιση"),
                (4, 2, "2026-08-24", None),
            ],
        )
        con.commit()
        con.close()

        for history_id in (2, 3, 4):
            with self.subTest(history_id=history_id):
                missing = self.client.post(
                    f"/api/appointment/new/{history_id}", json={}, headers=self.api_headers(),
                )
                self.assertEqual(missing.status_code, 400)
                response = self.new_session(history_id)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["amount_due"], 35.0)

    def test_missing_payment_uses_previous_session_charge(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE payments SET amount_due=22.5 WHERE appointment_id=1")
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) VALUES(2,1,2,'2026-08-22')"
        )
        con.commit()
        con.close()

        response = self.client.post(
            "/api/payment/ensure/2", json={}, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT amount_due FROM payments WHERE appointment_id=2"
            ).fetchone()[0],
            22.5,
        )
        con.close()

    def test_missing_first_payment_uses_social_security_charge(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE clinical_histories SET social_security='ΓεΣυ' WHERE history_id=1")
        con.execute("DELETE FROM payments WHERE appointment_id=1")
        con.commit()
        con.close()

        response = self.client.post(
            "/api/payment/ensure/1", json={}, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT amount_due FROM payments WHERE appointment_id=1"
            ).fetchone()[0],
            10.0,
        )
        con.close()

    def test_settings_explains_per_session_coverage_charge_rule(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Η κάλυψη και η χρέωση ορίζονται πλέον ανά συνεδρία", html)
        self.assertIn("Πλάνα κάλυψης", html)
        self.assertIn("Μηνιαίες τιμές ΓεΣΥ", html)
        self.assertNotIn("Χρησιμοποιείται η χρέωση της προηγούμενης συνεδρίας", html)
        self.assertIn('id="select-database"', html)
        self.assertIn('id="create-empty-database"', html)
        self.assertIn(self.db_path.name, html)

    def test_home_shows_database_and_startup_notice(self):
        client = self.app.test_client()
        first = client.get("/").get_data(as_text=True)
        self.assertIn('class="home-database"', first)
        self.assertIn(self.db_path.name, first)
        self.assertIn(str(self.db_path), first)
        self.assertIn('id="database-startup-dialog"', first)

        second = client.get("/").get_data(as_text=True)
        self.assertNotIn('id="database-startup-dialog"', second)

    def test_create_empty_database_copies_schema_without_data(self):
        destination = self.root / "empty-clinic.db"
        with mock.patch("app.choose_database_file", return_value=destination):
            response = self.client.post(
                "/api/database/create-empty", json={}, headers=self.api_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["path"], str(destination))
        self.assertTrue(destination.exists())

        source = sqlite3.connect(self.db_path)
        empty = sqlite3.connect(destination)
        try:
            source_schema = source.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
            empty_schema = empty.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
            self.assertEqual(empty_schema, source_schema)
            for table in (
                "patients", "clinical_histories", "appointments", "payments",
                "referrals", "professions", "doctors",
            ):
                self.assertEqual(empty.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(empty.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(empty.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            source.close()
            empty.close()

    def test_select_database_validates_switches_and_remembers_choice(self):
        other_db = self.root / "second-clinic.db"
        create_sample_db(other_db)
        con = sqlite3.connect(other_db)
        con.execute("UPDATE patients SET last_name='ΔΕΥΤΕΡΗ ΒΑΣΗ' WHERE patient_id=1")
        con.commit()
        con.close()

        with mock.patch("app.choose_database_file", return_value=other_db):
            response = self.client.post(
                "/api/database/select", json={}, headers=self.api_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(self.app.config["DB_PATH"]), other_db)
        self.assertEqual(
            json.loads(self.selection_path.read_text(encoding="utf-8"))["database_path"],
            str(other_db),
        )
        with mock.patch.dict("app.os.environ", {"PHYSIO_DB_PATH": ""}):
            self.assertEqual(startup_database_path(self.selection_path), other_db)
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn(other_db.name, home)
        self.assertIn('id="database-startup-dialog"', home)
        self.assertIn(
            "ΔΕΥΤΕΡΗ ΒΑΣΗ",
            self.client.get("/patients").get_data(as_text=True),
        )

    def test_invalid_database_selection_keeps_current_database(self):
        invalid = self.root / "invalid.db"
        invalid.write_text("not a sqlite database", encoding="utf-8")
        original = self.app.config["DB_PATH"]
        with mock.patch("app.choose_database_file", return_value=invalid):
            response = self.client.post(
                "/api/database/select", json={}, headers=self.api_headers(),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app.config["DB_PATH"], original)
        self.assertFalse(self.selection_path.exists())
        self.assertIn("ΖΗΝΩΝ", self.client.get("/patients").get_data(as_text=True))

    def test_non_gesy_plan_default_is_configurable_for_new_sessions(self):
        response = self.client.post(
            f"/api/coverage-plans/{self.self_standard_plan}", json={
                "name": "Αυτοπληρωμή", "default_charge": "40", "active": True,
            }, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        con.executemany(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,social_security,is_active,today) VALUES(?,?,?,?,1,0)",
            [(2, 2, "2026-08-22", "ΓΕΣΥ"), (3, 2, "2026-08-23", "Ιδιωτική")],
        )
        con.commit()
        con.close()
        for history_id in (2, 3):
            response = self.new_session(history_id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["amount_due"], 40.0)

    def test_dates_display_in_day_month_year_and_save_as_iso(self):
        patient = self.client.get("/patients/1").get_data(as_text=True)
        history = self.client.get("/histories/1").get_data(as_text=True)
        current = self.client.get("/current?history_id=1").get_data(as_text=True)
        self.assertIn('value="20/08/2026"', history)
        self.assertIn('value="21/08/2026"', current)
        self.assertIn('placeholder="DD/MM/YYYY"', patient)

        response = self.client.post("/api/update", json={
            "table": "patients", "pk": 1, "column": "birthdate", "value": "28/08/1980",
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        value = con.execute("SELECT birthdate FROM patients WHERE patient_id=1").fetchone()[0]
        con.close()
        self.assertEqual(value, "1980-08-28")

    def test_invalid_money_is_rejected_and_preserved(self):
        response = self.client.post("/api/update", json={
            "table": "payments", "pk": 1, "column": "amount_due", "value": "λάθος",
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 400)
        con = sqlite3.connect(self.db_path)
        value = con.execute("SELECT amount_due FROM payments WHERE payment_id=1").fetchone()[0]
        con.close()
        self.assertEqual(value, 35)

    def test_receipt_amount_is_real_money_and_updates_through_payment_api(self):
        con = sqlite3.connect(self.db_path)
        columns = {row[1]: row[2] for row in con.execute("PRAGMA table_info(payments)")}
        con.close()
        self.assertEqual(columns["receipt_amount"], "REAL")
        self.assertNotIn("receipt_number", columns)

        response = self.client.post("/api/update", json={
            "table": "payments", "pk": 1,
            "column": "receipt_amount", "value": "20,00",
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)

        con = sqlite3.connect(self.db_path)
        value, storage_type = con.execute(
            "SELECT receipt_amount, typeof(receipt_amount) FROM payments WHERE payment_id=1"
        ).fetchone()
        con.close()
        self.assertEqual(value, 20.0)
        self.assertEqual(storage_type, "real")

        html = self.client.get("/current?history_id=1").get_data(as_text=True)
        self.assertIn('data-column="receipt_amount" value="20"', html)
        self.assertIn(
            '<span>Αποδείξεις</span><strong data-session-total="receipts">20</strong>',
            html,
        )

    def test_text_plain_json_is_rejected(self):
        response = self.client.post(
            "/api/settings", data='{"default_amount_due":"99"}',
            content_type="text/plain", headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 415)

    def test_undo_is_scoped_to_database(self):
        other_db = self.root / "other.db"
        shutil.copy2(self.db_path, other_db)
        update = self.client.post("/api/update", json={
            "table": "patients", "pk": 1, "column": "first_name", "value": "ΑΛΛΑΓΗ",
        }, headers=self.api_headers())
        self.assertEqual(update.status_code, 200)

        other_app = create_app({
            "TESTING": True, "DB_PATH": str(other_db), "META_DB_PATH": str(self.meta_path),
            "BACKUP_DIR": str(self.backup_dir), "AUTO_BACKUP": False,
        })
        other_client = other_app.test_client()
        peek = other_client.get("/api/undo/peek")
        self.assertEqual(peek.status_code, 404)

    def test_greek_case_and_tonos_insensitive_search(self):
        response = self.client.get("/patients?q=αλεξ")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ΆΛΕΞ", response.get_data(as_text=True))

    def test_sort_controls_use_arrows_without_alphabetic_labels(self):
        response = self.client.get("/patients?sort=last_name&dir=asc")
        html = response.get_data(as_text=True)
        self.assertNotIn("A→Z", html)
        self.assertNotIn("Z→A", html)
        self.assertIn("↓", html)
        self.assertIn("Ταξινόμηση φθίνουσα", html)

    def test_greek_sorting_ignores_lowercase_uppercase_and_tonos(self):
        con = sqlite3.connect(self.db_path)
        con.executemany(
            "INSERT INTO patients(patient_id,first_name,last_name,is_active) VALUES(?,?,?,?)",
            [
                (3, "ΔΟΚΙΜΗ", "βήτα", 1),
                (4, "ΔΟΚΙΜΗ", "ΓΑΜΑ", 1),
            ],
        )
        con.executemany(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,is_active,today) VALUES(?,?,?,?,?)",
            [
                (2, 2, "2026-08-21", 1, 1),
                (3, 3, "2026-08-22", 1, 1),
                (4, 4, "2026-08-23", 1, 1),
            ],
        )
        con.commit()
        con.close()

        for path in (
            "/patients?sort=last_name&dir=asc",
            "/active?sort=last_name&dir=asc",
            "/current?view=today",
        ):
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                self.assertLess(html.index("ΑΝΔΡΕΟΥ"), html.index("βήτα"))
                self.assertLess(html.index("βήτα"), html.index("ΓΑΜΑ"))
                self.assertLess(html.index("ΓΑΜΑ"), html.index("ΖΗΝΩΝ"))

    def test_home_launcher_order_and_today_label(self):
        html = self.client.get("/").get_data(as_text=True)
        labels = [
            "ΝΕΟΣ ΑΣΘΕΝΗΣ", "ΑΣΘΕΝΕΙΣ", "ΝΕΟ ΙΣΤΟΡΙΚΟ",
            "ΙΣΤΟΡΙΚΑ", "ΣΗΜΕΡΙΝΑ ΙΣΤΟΡΙΚΑ", "ΣΗΜΕΡΑ", "ΣΤΑΤΙΣΤΙΚΑ",
        ]
        positions = [html.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        launcher_html = html[html.index('class="launcher-grid"'):html.index('class="keyboard-hint"')]
        self.assertNotIn(">ΑΥΤΟΣ<", launcher_html)

    def test_daily_label_replaces_autos_in_visible_pages(self):
        for path in ("/", "/current", "/active", "/patients/1", "/histories/1", "/histories/new?patient_id=1"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn("Ημερήσια", html)
                self.assertNotIn("ΑΥΤΟΣ", html)

    def test_new_forms_have_expected_autocomplete_fields(self):
        patient_html = self.client.get("/patients/new").get_data(as_text=True)
        patient_detail_html = self.client.get("/patients/1").get_data(as_text=True)
        history_html = self.client.get("/histories/new?patient_id=1").get_data(as_text=True)
        history_detail_html = self.client.get("/histories/1").get_data(as_text=True)
        patient_fields = ("first_name", "city", "referral", "profession")
        history_fields = (
            "main_diagnosis", "body_area", "doctor", "icd10_code",
        )
        for field in patient_fields:
            self.assertIn(f'data-autocomplete="{field}"', patient_html)
            self.assertIn(f'data-autocomplete="{field}"', patient_detail_html)
        self.assertEqual(patient_detail_html.count('class="autocomplete-toggle"'), 4)
        self.assertIn('data-autocomplete-create="profession"', patient_detail_html)
        self.assertNotIn('autocomplete="off"', patient_detail_html)
        self.assertIn('app.js?v=20260830-database-selection', patient_detail_html)
        self.assertNotIn('autocomplete="family-name"', patient_html)
        self.assertNotIn('autocomplete="street-address"', patient_html)
        self.assertNotIn('autocomplete="tel"', patient_html)
        self.assertNotIn('autocomplete="email"', patient_html)
        self.assertIn('autocomplete="off"', patient_html)
        for field in history_fields:
            self.assertIn(f'data-autocomplete="{field}"', history_html)
        for field in ("main_diagnosis", "body_area"):
            self.assertIn(f'data-autocomplete="{field}"', history_detail_html)
        self.assertEqual(history_detail_html.count('class="autocomplete-toggle"'), 2)

    def test_autocomplete_is_frequency_sorted_deduplicated_and_greek_insensitive(self):
        con = sqlite3.connect(self.db_path)
        con.executemany(
            "INSERT INTO patients(patient_id,first_name,last_name,is_active) VALUES(?,?,?,1)",
            [
                (3, "Άννα", "Α"), (4, "ΑΝΝΑ", "Β"), (5, "Άννα", "Γ"),
                (6, "Αναστασία", "Δ"),
            ],
        )
        con.commit()
        con.close()
        response = self.client.get("/api/autocomplete?field=first_name&q=αν")
        self.assertEqual(response.status_code, 200)
        suggestions = response.get_json()["suggestions"]
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]["frequency"], 3)
        self.assertEqual(suggestions[1]["frequency"], 1)
        self.assertLessEqual(len(suggestions), 15)

        unfiltered = self.client.get(
            "/api/autocomplete?field=first_name&q="
        ).get_json()["suggestions"]
        self.assertTrue(unfiltered)
        self.assertGreaterEqual(unfiltered[0]["frequency"], unfiltered[-1]["frequency"])

    def test_name_autocomplete_and_list_search_use_prefix_matching(self):
        con = sqlite3.connect(self.db_path)
        con.executemany(
            "INSERT INTO patients(patient_id,first_name,last_name,is_active) VALUES(?,?,?,1)",
            [(7, "Δημήτρης", "Α"), (8, "Ανδρέας", "Β")],
        )
        con.commit()
        con.close()

        suggestions = self.client.get(
            "/api/autocomplete?field=first_name&q=δ"
        ).get_json()["suggestions"]
        self.assertEqual([item["value"] for item in suggestions], ["Δημήτρης"])

        html = self.client.get("/patients?q=δ").get_data(as_text=True)
        self.assertIn("Δημήτρης", html)
        self.assertNotIn("Ανδρέας", html)

    def test_all_autocomplete_sources_use_actual_previous_values(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE patients SET city='Λευκωσία', referral_id=1, profession_id=1 WHERE patient_id=1"
        )
        con.execute(
            "UPDATE clinical_histories SET body_area='Ώμος', social_security='ΓεΣΥ', "
            "doctor_id=1, icd10_code='M25.5' WHERE history_id=1"
        )
        con.commit()
        con.close()
        queries = {
            "first_name": ("αλεξ", None),
            "city": ("λευκ", None),
            "referral": ("ιατρ", 1),
            "profession": ("εκπαι", 1),
            "main_diagnosis": ("ωμο", None),
            "body_area": ("ωμο", None),
            "social_security": ("γεσυ", None),
            "doctor": ("ιατρ", 1),
            "icd10_code": ("m25", None),
        }
        for field, (query, expected_id) in queries.items():
            with self.subTest(field=field):
                body = self.client.get(
                    f"/api/autocomplete?field={field}&q={query}"
                ).get_json()
                self.assertTrue(body["suggestions"])
                self.assertEqual(body["suggestions"][0]["id"], expected_id)
        doctor_by_first_name = self.client.get(
            "/api/autocomplete?field=doctor&q=μαρ"
        ).get_json()["suggestions"]
        self.assertEqual(doctor_by_first_name[0]["id"], 1)

    def test_new_related_values_are_saved_and_removed_by_undo(self):
        referrals_before = self.count("referrals")
        professions_before = self.count("professions")
        response = self.client.post("/patients/new", data={
            "csrf_token": self.csrf(), "first_name": "ΝΕΟΣ", "last_name": "ΣΧΕΣΕΙΣ",
            "referral_text": "Νέα παραπομπή", "profession_text": "Νέο επάγγελμα",
            "is_active": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.count("referrals"), referrals_before + 1)
        self.assertEqual(self.count("professions"), professions_before + 1)
        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.count("referrals"), referrals_before)
        self.assertEqual(self.count("professions"), professions_before)

    def test_existing_patient_can_create_new_profession_and_undo(self):
        professions_before = self.count("professions")
        response = self.client.post("/api/related-choice", json={
            "table": "patients", "pk": 1, "column": "profession_id",
            "kind": "profession", "value": "", "text": "Νέο επάγγελμα καρτέλας",
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        created_id = response.get_json()["value"]
        self.assertEqual(response.get_json()["display"], "Νέο επάγγελμα καρτέλας")
        self.assertEqual(self.count("professions"), professions_before + 1)

        con = sqlite3.connect(self.db_path)
        saved_id = con.execute(
            "SELECT profession_id FROM patients WHERE patient_id=1"
        ).fetchone()[0]
        con.close()
        self.assertEqual(saved_id, created_id)

        suggestions = self.client.get(
            "/api/autocomplete?field=profession&q=νεο επαγγελμα καρτελας"
        ).get_json()["suggestions"]
        self.assertEqual(suggestions[0]["id"], created_id)

        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.count("professions"), professions_before)
        con = sqlite3.connect(self.db_path)
        restored_id = con.execute(
            "SELECT profession_id FROM patients WHERE patient_id=1"
        ).fetchone()[0]
        con.close()
        self.assertIsNone(restored_id)

    def test_existing_patient_free_text_reuses_existing_profession(self):
        professions_before = self.count("professions")
        response = self.client.post("/api/related-choice", json={
            "table": "patients", "pk": 1, "column": "profession_id",
            "kind": "profession", "value": "", "text": "εκπαιδευτικός",
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["value"], 1)
        self.assertEqual(self.count("professions"), professions_before)

    def test_new_doctor_value_is_saved_and_removed_by_undo(self):
        doctors_before = self.count("doctors")
        response = self.client.post("/histories/new", data={
            "csrf_token": self.csrf(), "patient_id": "1",
            "history_date": "2026-08-28", "main_diagnosis": "Δοκιμή",
            "doctor_text": "Νέος ιατρός", "is_active": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.count("doctors"), doctors_before + 1)
        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.count("doctors"), doctors_before)

    def test_lists_use_automatic_batches_without_numbered_pages(self):
        con = sqlite3.connect(self.db_path)
        con.executemany(
            "INSERT INTO patients(patient_id,first_name,last_name,is_active) VALUES(?,?,?,1)",
            [(patient_id, f"ΟΝΟΜΑ{patient_id}", "ΜΑΖΙΚΟΣ") for patient_id in range(100, 190)],
        )
        con.commit()
        con.close()

        first = self.client.get("/patients?sort=patient_id&dir=asc")
        first_html = first.get_data(as_text=True)
        self.assertEqual(first_html.count("data-list-row"), 75)
        self.assertNotIn("Σελίδα 1", first_html)
        self.assertIn("data-has-more=\"true\"", first_html)

        second = self.client.get(
            "/patients?sort=patient_id&dir=asc&offset=75&format=json"
        ).get_json()
        self.assertEqual(second["count"], 17)
        self.assertFalse(second["has_more"])

        searched = self.client.get("/patients?q=ΟΝΟΜΑ189").get_data(as_text=True)
        self.assertIn("ΟΝΟΜΑ189", searched)

        descending = self.client.get("/patients?sort=patient_id&dir=desc").get_data(as_text=True)
        self.assertLess(descending.index("<td>189</td>"), descending.index("<td>188</td>"))

        for path in ("/patients", "/active", "/activation"):
            with self.subTest(path=path):
                empty_batch = self.client.get(
                    f"{path}?offset=999999&format=json"
                )
                self.assertEqual(empty_batch.status_code, 200)
                self.assertEqual(empty_batch.get_json()["count"], 0)

    def test_patient_list_shows_and_sorts_history_counts(self):
        descending = self.client.get(
            "/patients?sort=history_count&dir=desc"
        ).get_data(as_text=True)
        self.assertIn("Ιστορικά/Ενεργά", descending)
        self.assertIn(">1/1<", descending)
        self.assertIn(">0/0<", descending)
        self.assertLess(
            descending.index('data-row-open="/patients/1"'),
            descending.index('data-row-open="/patients/2"'),
        )

        ascending = self.client.get(
            "/patients?sort=history_count&dir=asc"
        ).get_data(as_text=True)
        self.assertLess(
            ascending.index('data-row-open="/patients/2"'),
            ascending.index('data-row-open="/patients/1"'),
        )

    def test_patient_list_active_column_uses_editable_checkboxes(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE patients SET is_active=0 WHERE patient_id=2")
        con.commit()
        con.close()

        html = self.client.get("/patients?sort=patient_id&dir=asc").get_data(as_text=True)
        self.assertIn(
            'data-table="patients" data-pk="1" data-column="is_active" value="1" '
            'aria-label="Ενεργός ασθενής 1" checked',
            html,
        )
        self.assertIn(
            'data-table="patients" data-pk="2" data-column="is_active" value="1" '
            'aria-label="Ενεργός ασθενής 2" >',
            html,
        )
        header = html.split("<thead><tr>", 1)[1].split("</tr></thead>", 1)[0]
        self.assertNotIn("Ενέργεια", header)
        self.assertNotIn(">Άνοιγμα<", html)
        self.assertLess(header.index("Κινητό"), header.index("Αριθμός Ταυτότητας"))
        self.assertLess(header.index("Αριθμός Ταυτότητας"), header.index("Γέννηση"))

        picker = self.client.get("/patients?choose_for_history=1").get_data(as_text=True)
        self.assertIn("Ενέργεια", picker)
        self.assertIn(">Νέο ιστορικό<", picker)

    def test_activation_page_lists_active_and_inactive_histories(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,main_diagnosis,is_active,today) "
            "VALUES(2,2,'2026-08-19','Ανενεργό ιστορικό',0,0)"
        )
        con.commit()
        con.close()

        html = self.client.get("/activation?sort=history_id&dir=asc").get_data(as_text=True)
        self.assertIn("Ενεργοποίηση / Απενεργοποίηση", html)
        self.assertIn('data-history="1"', html)
        self.assertIn('data-history="2"', html)
        self.assertIn('data-history="1" aria-label="Ενεργό ιστορικό 1" checked', html)
        self.assertIn('data-history="2" aria-label="Ενεργό ιστορικό 2" ', html)

    def test_histories_page_lists_active_and_inactive_histories(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,main_diagnosis,is_active,today) "
            "VALUES(2,2,'2026-08-19','Ανενεργό ιστορικό',0,0)"
        )
        con.commit()
        con.close()

        html = self.client.get("/active?sort=history_id&dir=asc").get_data(as_text=True)
        self.assertIn("<title>Ιστορικά — ΦΥΣΙΟ</title>", html)
        self.assertIn('href="/histories/1"', html)
        self.assertIn('href="/histories/2"', html)
        self.assertIn('data-row-open="/histories/1"', html)
        self.assertIn('data-row-open="/histories/2"', html)
        self.assertIn("Όλα τα ενεργά και ανενεργά ιστορικά.", html)
        header = html.split("<thead><tr>", 1)[1].split("</tr></thead>", 1)[0]
        self.assertLess(header.index("Επώνυμο"), header.index("Ενεργή επαφή"))
        self.assertLess(header.index("Ενεργή επαφή"), header.index("Ενεργό ιστορικό"))
        self.assertLess(header.index("Ενεργό ιστορικό"), header.index("Ημερήσια"))
        self.assertLess(header.index("Ημερήσια"), header.index("Ημερομηνία ιστορικού"))
        self.assertNotIn("Ενέργειες", header)

        script = (Path(self.app.static_folder) / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("event.target.closest('tr[data-row-open]')", script)
        self.assertIn("window.location.assign(row.dataset.rowOpen)", script)

    def test_activation_page_keeps_three_sort_levels_and_directions(self):
        con = sqlite3.connect(self.db_path)
        con.executemany(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,main_diagnosis,is_active,today) "
            "VALUES(?,?,?,?,0,0)",
            [
                (2, 2, "2026-08-19", "Δεύτερο",),
                (3, 1, "2026-08-18", "Τρίτο",),
                (4, 2, "2026-08-17", "Τέταρτο",),
            ],
        )
        con.commit()
        con.close()

        html = self.client.get(
            "/activation?sort=history_active,patient_id,history_id&dir=asc,desc,desc"
        ).get_data(as_text=True)
        positions = [html.index(f'data-history="{history_id}"') for history_id in (4, 2, 3, 1)]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('class="sort-priority"', html)

    def test_current_summary_keeps_only_diagnosis(self):
        html = self.client.get("/current?history_id=1").get_data(as_text=True)
        summary = html.split('<div class="summary-line">', 1)[1].split(
            '</div><label class="history-comments">', 1
        )[0]
        self.assertIn("Ώμος", summary)
        self.assertEqual(summary.count("<span>"), 1)
        self.assertNotIn("#1", summary)
        self.assertNotIn("Ναι", summary)

    def test_current_history_comments_are_editable_and_presence_delete_requires_selection(self):
        html = self.client.get("/current?history_id=1").get_data(as_text=True)
        self.assertIn(
            'class="problem-box autosave" data-table="clinical_histories" '
            'data-pk="1" data-column="problem_description"',
            html,
        )
        self.assertIn(
            '<button id="delete-appointment" class="danger-btn" type="button" disabled>'
            'Διαγραφή παρουσίας</button>',
            html,
        )
        self.assertIn('data-appointment="1" tabindex="0" aria-selected="false"', html)
        javascript = (Path(__file__).parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Θα διαγραφεί η επιλεγμένη παρουσία και η πληρωμή της.", javascript)

    def test_current_shows_session_totals_between_title_and_new_button(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) VALUES(2,1,2,'2026-08-22')"
        )
        con.execute(
            "INSERT INTO payments(appointment_id,payment_date,amount_due,amount_paid,receipt_amount) VALUES(2,'2026-08-22',15.5,10,5)"
        )
        con.commit()
        con.close()

        html = self.client.get("/current?history_id=1").get_data(as_text=True)
        toolbar = html.split('<div class="session-toolbar">', 1)[1].split("</div>\n    <div class=\"table-wrap", 1)[0]
        self.assertLess(toolbar.index("Συνεδρίες"), toolbar.index('class="session-totals"'))
        self.assertLess(toolbar.index('class="session-totals"'), toolbar.index('id="new-appointment"'))
        self.assertIn('<span>Χρέωση</span><strong data-session-total="due">51</strong>', toolbar)
        self.assertIn('<span>Πίστωση</span><strong data-session-total="credit">10</strong>', toolbar)
        self.assertIn('<span>Αποδείξεις</span><strong data-session-total="receipts">5</strong>', toolbar)
        self.assertNotIn("Σύνολο Χρέωσης", toolbar)

    def test_current_filters_keep_direct_views_but_show_only_daily_and_active_histories(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO patients(patient_id,first_name,last_name,is_active) VALUES(3,'ΑΝΕΝΕΡΓΟΣ','ΑΣΘΕΝΗΣ',0)"
        )
        con.executemany(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,main_diagnosis,is_active,today) VALUES(?,?,?,?,?,?)",
            [
                (2, 1, "2026-08-21", "Ανενεργό ιστορικό ενεργού ασθενή", 0, 1),
                (3, 2, "2026-08-22", "Ενεργό ιστορικό ενεργού ασθενή", 1, 0),
                (4, 3, "2026-08-23", "Ενεργό ιστορικό ανενεργού ασθενή", 1, 1),
            ],
        )
        con.commit()
        con.close()

        expected_ids = {
            "/current": {1},
            "/current?view=active_histories": {1, 3, 4},
            "/current?view=active_patients": {1, 2, 3},
            "/current?view=all": {1, 2, 3, 4},
        }
        for path, included_ids in expected_ids.items():
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                for history_id in range(1, 5):
                    marker = f'data-history-id="{history_id}"'
                    if history_id in included_ids:
                        self.assertIn(marker, html)
                    else:
                        self.assertNotIn(marker, html)

        selected_html = self.client.get("/current?history_id=2").get_data(as_text=True)
        self.assertIn('id="new-appointment" class="primary-btn" data-history="2"', selected_html)
        self.assertNotIn('data-history-id="2"', selected_html)

        default_html = self.client.get("/current").get_data(as_text=True)
        for key, label in (("today", "Ημερήσια"), ("active_histories", "Ενεργά ιστορικά")):
            self.assertIn(f'data-current-filter="{key}"', default_html)
            self.assertIn(label, default_html)
        self.assertNotIn('data-current-filter="active_patients"', default_html)
        self.assertNotIn('data-current-filter="all"', default_html)
        self.assertNotIn("Ενεργός ασθενής", default_html)
        self.assertNotIn("Όλες οι εγγραφές", default_html)
        self.assertIn('data-current-filter="today" href="/current?view=today" aria-current="page"', default_html)

    def test_deactivating_history_clears_today_and_undo_restores_both(self):
        response = self.client.post(
            "/api/deactivate/1", json={}, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT is_active,today FROM clinical_histories WHERE history_id=1"
            ).fetchone(),
            (0, 0),
        )
        con.close()

        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT is_active,today FROM clinical_histories WHERE history_id=1"
            ).fetchone(),
            (1, 1),
        )
        con.close()

    def test_deactivating_patient_clears_all_today_flags_and_undo_restores_them(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,is_active,today) VALUES(2,1,'2026-08-22',1,1)"
        )
        con.commit()
        con.close()

        response = self.client.post("/api/update", json={
            "table": "patients", "pk": 1, "column": "is_active", "value": 0,
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute("SELECT is_active FROM patients WHERE patient_id=1").fetchone()[0],
            0,
        )
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) FROM clinical_histories WHERE patient_id=1 AND today=1"
            ).fetchone()[0],
            0,
        )
        con.close()

        rejected = self.client.post("/api/update", json={
            "table": "clinical_histories", "pk": 1, "column": "today", "value": 1,
        }, headers=self.api_headers())
        self.assertEqual(rejected.status_code, 400)

        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute("SELECT is_active FROM patients WHERE patient_id=1").fetchone()[0],
            1,
        )
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) FROM clinical_histories WHERE patient_id=1 AND today=1"
            ).fetchone()[0],
            2,
        )
        con.close()

    def test_history_autosave_deactivation_clears_today_and_inactive_records_cannot_restore_it(self):
        response = self.client.post("/api/update", json={
            "table": "clinical_histories", "pk": 1, "column": "is_active", "value": 0,
        }, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT is_active,today FROM clinical_histories WHERE history_id=1"
            ).fetchone(),
            (0, 0),
        )
        con.close()

        rejected = self.client.post("/api/update", json={
            "table": "clinical_histories", "pk": 1, "column": "today", "value": 1,
        }, headers=self.api_headers())
        self.assertEqual(rejected.status_code, 400)

    def test_new_inactive_history_forces_today_off(self):
        response = self.client.post("/histories/new", data={
            "csrf_token": self.csrf(), "patient_id": "1", "history_date": "29/08/2026",
            "main_diagnosis": "Ανενεργό", "today": "1",
        })
        self.assertEqual(response.status_code, 302)
        history_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT is_active,today FROM clinical_histories WHERE history_id=?", (history_id,)
            ).fetchone(),
            (0, 0),
        )
        con.close()

    def test_current_filter_header_stays_visible_while_record_list_scrolls(self):
        stylesheet = (
            Path(__file__).parents[1] / "static" / "css" / "app.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".current-patients { position: sticky;", stylesheet)
        self.assertIn("display: flex; flex-direction: column; overflow: hidden;", stylesheet)
        self.assertIn(".current-list { flex: 1 1 auto; min-height: 0; overflow-y: auto;", stylesheet)

    def test_screen_actions_and_shared_dirty_form_protection_are_rendered(self):
        for path in ("/patients", "/patients/new", "/patients/1", "/histories/1", "/settings"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('class="screen-actions"', html)
            self.assertIn('aria-label="Επιστροφή"', html)
            self.assertIn('aria-label="Κλείσιμο και επιστροφή στην αρχική"', html)
            header = html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
            self.assertLess(header.index('class="screen-actions"'), header.index('id="save-status"'))
        new_patient = self.client.get("/patients/new").get_data(as_text=True)
        self.assertIn("data-dirty-form", new_patient)
        javascript = (Path(__file__).parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("beforeunload", javascript)
        self.assertIn("Υπάρχουν αλλαγές που δεν έχουν αποθηκευτεί", javascript)

    def test_save_status_is_in_topbar_and_tables_have_vertical_lines(self):
        html = self.client.get("/patients").get_data(as_text=True)
        header_start = html.index('class="topbar"')
        header_end = html.index("</header>", header_start)
        status_position = html.index('id="save-status"')
        self.assertLess(header_start, status_position)
        self.assertLess(status_position, header_end)

        stylesheet = (
            Path(__file__).parents[1] / "static" / "css" / "app.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".data-table th:not(:last-child)", stylesheet)
        self.assertIn(".data-table td:not(:last-child)", stylesheet)
        self.assertIn(".sticky-table-header { position: fixed;", stylesheet)
        javascript = (Path(__file__).parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("initializePersistentTableHeaders", javascript)
        self.assertIn("fixedTable.style.transform = `translateX(${-wrap.scrollLeft}px)`", javascript)

    def test_current_header_is_one_six_field_row_and_comments_are_compact(self):
        html = self.client.get("/current?history_id=1").get_data(as_text=True)
        header = html.split('<div class="current-header-grid">', 1)[1].split("</div>\n    <div class=\"history-summary\"", 1)[0]
        for label in (
            "Όνομα", "Επώνυμο", "Ημερομηνία γέννησης", "Αριθμός ταυτότητας",
            "Legacy κοινωνική ασφάλιση", "Legacy παραπεμπτικό",
        ):
            self.assertIn(f'<span class="label">{label}</span>', header)
        for removed_label in (
            "Ασθενής ID", "Ιστορικό ID", "Κινητό", "Ημερομηνία ιστορικού", "Ενεργό ιστορικό",
        ):
            self.assertNotIn(f'<span class="label">{removed_label}</span>', header)
        self.assertEqual(header.count('<span class="label">'), 6)

        stylesheet = (Path(__file__).parents[1] / "static" / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".current-header-grid { display: grid; grid-template-columns: repeat(6,", stylesheet)
        self.assertIn(".history-comments { display: block; margin-top: 3px;", stylesheet)
        self.assertIn("min-height: 90px", stylesheet)

    def test_all_spreadsheet_screens_show_matching_and_database_record_counts(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,main_diagnosis,is_active,today) "
            "VALUES(2,2,'2026-08-22','Γόνατο',0,0)"
        )
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) "
            "VALUES(2,2,1,'2026-08-22')"
        )
        con.commit()
        con.close()

        expected = {
            "/patients?q=αλ": (1, 2, "1 εγγραφή από 2"),
            "/active": (2, 2, "2 εγγραφές από 2"),
            "/activation": (2, 2, "2 εγγραφές από 2"),
            "/today?date=21/08/2026": (1, 2, "1 εγγραφή από 2"),
            "/current?history_id=1": (1, 2, "1 εγγραφή από 2"),
        }
        for path, (visible, total, label) in expected.items():
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                self.assertIn(
                    f'data-record-count data-visible-count="{visible}" '
                    f'data-total-count="{total}"',
                    html,
                )
                self.assertIn(label, html)
                heading_area = (
                    html.split('<div class="session-toolbar">', 1)[1].split(
                        '<div class="table-wrap sessions-wrap">', 1,
                    )[0]
                    if path.startswith("/current")
                    else html.split('<div class="panel-header">', 1)[1].split(
                        '<div class="table-wrap">', 1,
                    )[0]
                )
                self.assertIn("data-record-count", heading_area)
                table_area = html.split('<div class="table-wrap', 1)[1]
                self.assertNotIn(
                    'class="table-footer">\n    <div class="record-count"',
                    table_area,
                )

    def test_topbar_has_requested_navigation_order(self):
        html = self.client.get("/patients").get_data(as_text=True)
        nav = html.split('<nav id="main-nav">', 1)[1].split("</nav>", 1)[0]
        labels = ["Ασθενείς", "Ιστορικά", "Ημερήσια", "Σήμερα", "Ρυθμίσεις", "Αναίρεση"]
        positions = [nav.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Ενεργά ιστορικά", nav)
        self.assertNotIn("Ενεργοποίηση", nav)

    def test_statistics_launcher_opens_statistics_home(self):
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/statistics"><strong>ΣΤΑΤΙΣΤΙΚΑ</strong>', home)
        statistics = self.client.get("/statistics").get_data(as_text=True)
        today = date.today()
        month_start = today.replace(day=1)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        month_end = next_month - timedelta(days=1)
        self.assertIn("<title>Στατιστικά — ΦΥΣΙΟ</title>", statistics)
        self.assertIn("Συγκεντρωτικά στοιχεία από πραγματικές επισκέψεις", statistics)
        self.assertIn("<span>Επισκέψεις</span>", statistics)
        self.assertIn("Επισκέψεις ανά ιστορικό", statistics)
        self.assertIn(f'name="from" value="{month_start.isoformat()}"', statistics)
        self.assertIn(f'name="to" value="{month_end.isoformat()}"', statistics)
        all_periods = self.client.get("/statistics?year=&from=&to=&top=10").get_data(as_text=True)
        self.assertIn('name="from" value=""', all_periods)
        self.assertIn('name="to" value=""', all_periods)
        self.assertEqual(self.client.get("/statistics?year=2026").status_code, 200)
        self.assertEqual(self.client.get("/statistics?from=2026-08-31&to=2026-08-01").status_code, 400)

    def test_today_screen_uses_selected_appointment_date_and_includes_inactive_records(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,social_security,gesy_referral,is_active,today) "
            "VALUES(2,2,'2026-08-19','Ιδιωτική','Γ123',0,0)"
        )
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) "
            "VALUES(2,2,1,'2026-08-21')"
        )
        con.execute(
            "INSERT INTO appointments(appointment_id,history_id,appointment_number,appointment_date) "
            "VALUES(3,2,2,'2026-08-21')"
        )
        con.commit()
        con.close()

        html = self.client.get("/today?date=21/08/2026").get_data(as_text=True)
        self.assertIn("Σήμερα — 21/08/2026", html)
        header = html.split("<thead><tr>", 1)[1].split("</tr></thead>", 1)[0]
        labels = [
            "Ασθενής ID", "Ιστορικό ID", "Επώνυμο", "Όνομα",
            "Κάλυψη", "Παραπεμπτικό ΓεΣΥ", "Χρέωση", "Συμπληρωμή",
            "Είσπραξη", "Απόδειξη",
        ]
        positions = [header.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(html.count('href="/histories/2"'), 2)
        self.assertIn("Legacy / άγνωστη", html)
        self.assertIn('name="date" class="date-input" inputmode="numeric" placeholder="DD/MM/YYYY" value="21/08/2026"', html)
        self.assertIn('id="today-calendar-button" class="calendar-picker" type="button"', html)
        self.assertIn('id="today-calendar-popup" class="today-calendar-popup" hidden', html)
        self.assertIn('<section class="panel today-page-panel">', html)
        self.assertIn(
            'id="today-native-date" type="date" value="2026-08-21" '
            'data-default-date="2026-08-21"',
            html,
        )
        script = (Path(self.app.static_folder) / "js" / "app.js").read_text(encoding="utf-8")
        stylesheet = (Path(self.app.static_folder) / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".today-page-panel { overflow: visible; }", stylesheet)
        self.assertIn("renderTodayCalendar", script)
        self.assertIn("todayNativeDate?.dataset.defaultDate", script)
        self.assertIn("legacyPicker.replaceWith(todayCalendarButton)", script)
        self.assertIn('aria-sort="ascending"', header)

        empty = self.client.get("/today?date=22/08/2026").get_data(as_text=True)
        self.assertIn("Δεν υπάρχουν καταχωρημένα ραντεβού", empty)

    def test_validation_errors_only_mark_meaningful_submitted_values_dirty(self):
        unchanged = self.client.post("/patients/new", data={
            "csrf_token": self.csrf(), "is_active": "1",
        })
        self.assertEqual(unchanged.status_code, 400)
        self.assertIn('data-unsaved="false"', unchanged.get_data(as_text=True))

        changed = self.client.post("/patients/new", data={
            "csrf_token": self.csrf(), "first_name": "Δοκιμή",
            "birthdate": "όχι-ημερομηνία", "is_active": "1",
        })
        self.assertEqual(changed.status_code, 400)
        self.assertIn('data-unsaved="true"', changed.get_data(as_text=True))

    def test_future_appointments_create_all_configured_durations(self):
        for index, duration in enumerate((30, 45, 60)):
            with self.subTest(duration=duration):
                response = self.future_appointment(
                    appointment_date=f"2026-09-{7 + index:02d}",
                    duration_minutes=duration,
                )
                self.assertEqual(response.status_code, 200)
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT duration_minutes,status FROM Future_appointments "
            "ORDER BY future_appointment_id"
        ).fetchall()
        con.close()
        self.assertEqual(rows, [(30, "scheduled"), (45, "scheduled"), (60, "scheduled")])

    def test_future_appointment_quiet_hours_and_adjacent_slots_are_allowed(self):
        first = self.future_appointment(
            appointment_date="07/09/2026", start_time="13:00", duration_minutes=60,
        )
        adjacent = self.future_appointment(
            start_time="14:00", duration_minutes=60, confirm_second=True,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(adjacent.status_code, 200)

    def test_future_appointment_overlap_is_rejected(self):
        self.assertEqual(
            self.future_appointment(start_time="10:00", duration_minutes=30).status_code,
            200,
        )
        response = self.future_appointment(
            start_time="10:15", duration_minutes=45, confirm_second=True,
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("συνεχόμενο διάστημα", response.get_json()["error"])
        self.assertEqual(self.count("Future_appointments"), 1)

    def test_second_same_patient_appointment_warns_with_time_then_confirms(self):
        first = self.future_appointment(start_time="08:30", duration_minutes=30)
        warning = self.future_appointment(start_time="09:30", duration_minutes=30)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(warning.status_code, 409)
        self.assertTrue(warning.get_json()["requires_confirmation"])
        self.assertIn("08:30–09:00", warning.get_json()["message"])
        confirmed = self.future_appointment(
            start_time="09:30", duration_minutes=30, confirm_second=True,
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(self.count("Future_appointments"), 2)

    def test_history_of_another_patient_is_rejected(self):
        response = self.future_appointment(patient_id=2, history_id=1)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.count("Future_appointments"), 0)

    def test_cancelled_future_appointment_is_retained_with_cancelled_status(self):
        created = self.future_appointment()
        appointment_id = created.get_json()["future_appointment_id"]
        cancelled = self.client.post(
            f"/api/future-appointments/{appointment_id}/cancel",
            json={}, headers=self.api_headers(),
        )
        self.assertEqual(cancelled.status_code, 200)
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT status FROM Future_appointments WHERE future_appointment_id=?",
            (appointment_id,),
        ).fetchone()
        con.close()
        self.assertEqual(row, ("cancelled",))
        self.assertEqual(self.count("Future_appointments"), 1)

    def test_future_appointment_can_be_edited_and_status_is_validated(self):
        created = self.future_appointment()
        appointment_id = created.get_json()["future_appointment_id"]
        changed = self.client.post(
            f"/api/future-appointments/{appointment_id}",
            json={
                "appointment_date": "2026-09-08", "start_time": "15:15",
                "duration_minutes": 45, "status": "no_show", "notes": "Νέα σημείωση",
            },
            headers=self.api_headers(),
        )
        self.assertEqual(changed.status_code, 200)
        invalid = self.client.post(
            f"/api/future-appointments/{appointment_id}",
            json={
                "appointment_date": "2026-09-08", "start_time": "15:15",
                "duration_minutes": 45, "status": "deleted",
            },
            headers=self.api_headers(),
        )
        self.assertEqual(invalid.status_code, 400)
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT appointment_date,start_time,duration_minutes,status,notes "
            "FROM Future_appointments WHERE future_appointment_id=?",
            (appointment_id,),
        ).fetchone()
        con.close()
        self.assertEqual(row, ("2026-09-08", "15:15", 45, "no_show", "Νέα σημείωση"))

    def test_future_appointment_settings_control_durations_and_default(self):
        saved = self.client.post(
            "/api/settings",
            json={"appointment_settings": {
                "calendar_start": "08:00", "calendar_end": "20:00",
                "morning_end": "13:00", "afternoon_start": "15:00",
                "step": 15, "durations": [45], "default_duration": 45,
            }},
            headers=self.api_headers(),
        )
        self.assertEqual(saved.status_code, 200)
        rejected = self.future_appointment(duration_minutes=30)
        accepted = self.future_appointment(duration_minutes=45)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        html = self.client.get(
            "/future-appointments?view=day&date=2026-09-07"
        ).get_data(as_text=True)
        self.assertIn('data-duration="45" aria-pressed="true">45′</button>', html)
        self.assertNotIn('data-duration="30"', html)

    def test_future_calendar_views_navigation_sidebar_and_preselection(self):
        week = self.client.get(
            "/future-appointments?view=week&date=2026-09-09&patient_id=1&history_id=1"
        )
        self.assertEqual(week.status_code, 200)
        html = week.get_data(as_text=True)
        self.assertIn("Ενεργοί ασθενείς / ιστορικά", html)
        self.assertIn("Πρόσφατοι", html)
        self.assertIn('data-history="1"', html)
        self.assertIn('data-time="13:00"', html)
        self.assertIn('class="calendar-slot quiet-hours"', html)
        self.assertIn('placeholder="DD/MM/YYYY"', html)
        self.assertNotIn('id="future-date" type="date"', html)
        self.assertIn('placeholder="ΩΩ:ΛΛ"', html)
        self.assertIn('class="calendar-grid calendar-days-6"', html)
        self.assertNotIn('style="--days:', html)
        self.assertIn('class="calendar-time calendar-end-time">20:00</div>', html)
        self.assertEqual(html.count('class="calendar-end-cell"'), 6)
        self.assertIn('id="close-future-appointment"', html)
        self.assertIn('id="cancel-future-appointment"', html)
        self.assertEqual(html.count('class="calendar-day-head"'), 6)
        self.assertNotIn('<strong>Κυρ</strong>', html)
        day = self.client.get("/future-appointments?view=day&date=2026-09-09")
        self.assertEqual(day.status_code, 200)
        day_html = day.get_data(as_text=True)
        self.assertEqual(day_html.count('class="calendar-day-head"'), 1)
        self.assertIn('class="calendar-grid calendar-days-1"', day_html)
        stylesheet = (Path(self.app.static_folder) / "css" / "app.css").read_text(encoding="utf-8")
        javascript = (Path(self.app.static_folder) / "js" / "future_appointments.js").read_text(encoding="utf-8")
        self.assertIn(".calendar-grid.calendar-days-6", stylesheet)
        self.assertIn(".appointment-block.slot-span-4", stylesheet)
        self.assertIn("appointment-preview", javascript)
        self.assertNotIn("style.setProperty", javascript)

    def test_next_appointment_shortcuts_preserve_patient_and_history(self):
        expected = "/future-appointments?patient_id=1&amp;history_id=1"
        current = self.client.get("/current?history_id=1").get_data(as_text=True)
        history = self.client.get("/histories/1").get_data(as_text=True)
        self.assertIn("Επόμενο ραντεβού", current)
        self.assertIn(expected, current)
        self.assertIn("Νέο ραντεβού", history)
        self.assertIn(expected, history)

    def test_manual_backup(self):
        before = len(list(self.backup_dir.glob("*.db")))
        response = self.client.post("/api/backup", json={}, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(self.backup_dir.glob("*.db"))), before + 1)

    def test_security_headers_and_csrf(self):
        response = self.client.get("/patients")
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        rejected = self.client.post("/api/update", json={})
        self.assertEqual(rejected.status_code, 400)


    def test_gesy_referral_tracks_six_completed_visits_and_blocks_seventh(self):
        referral_id = self.create_gesy_referral(visits=6)
        for day in range(1, 7):
            response = self.new_session(
                1, appointment_date=f"2026-09-{day:02d}",
                coverage_plan_id=self.gesy_plan,
                gesy_referral_id=referral_id, copayment=10 if day <= 3 else 0,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["referral_exhausted"], day == 6)
        blocked = self.new_session(
            1, appointment_date="2026-09-07", coverage_plan_id=self.gesy_plan,
            gesy_referral_id=referral_id, copayment=0,
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("6/6", blocked.get_json()["error"])
        html = self.client.get("/histories/1").get_data(as_text=True)
        self.assertIn("6/6", html)

    def test_cancelled_and_scheduled_future_appointments_do_not_consume_referral(self):
        referral_id = self.create_gesy_referral(visits=3)
        created = self.future_appointment(
            coverage_plan_id=self.gesy_plan, gesy_referral_id=referral_id,
        )
        self.assertEqual(created.status_code, 200)
        future_id = created.get_json()["future_appointment_id"]
        cancelled = self.client.post(
            f"/api/future-appointments/{future_id}/cancel", json={},
            headers=self.api_headers(),
        )
        self.assertEqual(cancelled.status_code, 200)
        con = sqlite3.connect(self.db_path)
        used = con.execute(
            "SELECT COUNT(*) FROM appointments WHERE gesy_referral_id=? AND status='completed'",
            (referral_id,),
        ).fetchone()[0]
        con.close()
        self.assertEqual(used, 0)

    def test_future_completion_creates_one_performed_appointment_and_payment(self):
        referral_id = self.create_gesy_referral(visits=3)
        created = self.future_appointment(
            coverage_plan_id=self.gesy_plan, gesy_referral_id=referral_id,
        )
        future_id = created.get_json()["future_appointment_id"]
        completed = self.client.post(
            f"/api/future-appointments/{future_id}", json={
                "appointment_date": "2026-09-07", "start_time": "08:00",
                "duration_minutes": 60, "status": "completed",
                "coverage_plan_id": self.gesy_plan,
                "gesy_referral_id": referral_id, "copayment": 10,
            }, headers=self.api_headers(),
        )
        self.assertEqual(completed.status_code, 200)
        appointment_id = completed.get_json()["completed_appointment_id"]
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute(
            "SELECT coverage_plan_id,gesy_referral_id FROM appointments WHERE appointment_id=?",
            (appointment_id,),
        ).fetchone(), (self.gesy_plan, referral_id))
        self.assertEqual(con.execute(
            "SELECT amount_due,copayment,amount_paid,receipt_amount FROM payments WHERE appointment_id=?",
            (appointment_id,),
        ).fetchone(), (None, 10.0, 0.0, 0.0))
        con.close()

    def test_second_referral_and_switch_to_self_or_private_are_preserved_per_session(self):
        first_referral = self.create_gesy_referral(number="R1", visits=1)
        second_referral = self.create_gesy_referral(number="R2", visits=6)
        self.assertNotEqual(first_referral, second_referral)
        first = self.new_session(
            1, appointment_date="2026-09-01", coverage_plan_id=self.gesy_plan,
            gesy_referral_id=first_referral, copayment=10,
        )
        self.assertTrue(first.get_json()["referral_exhausted"])
        self.assertEqual(self.new_session(
            1, appointment_date="2026-09-02", coverage_plan_id=self.self_standard_plan,
        ).status_code, 200)
        con = sqlite3.connect(self.db_path)
        private_plan = con.execute(
            "SELECT coverage_plan_id FROM CoveragePlans WHERE code='PRIVATE_STANDARD'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(self.new_session(
            1, appointment_date="2026-09-03", coverage_plan_id=private_plan,
        ).status_code, 200)
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT coverage_plan_id,gesy_referral_id FROM appointments "
            "WHERE appointment_date BETWEEN '2026-09-01' AND '2026-09-03' ORDER BY appointment_date"
        ).fetchall()
        con.close()
        self.assertEqual(rows, [
            (self.gesy_plan, first_referral),
            (self.self_standard_plan, None),
            (private_plan, None),
        ])

    def test_gesy_copayment_is_a_per_session_snapshot(self):
        referral_id = self.create_gesy_referral()
        for day, copayment in ((1, 10), (2, 0)):
            self.assertEqual(self.new_session(
                1, appointment_date=f"2026-09-{day:02d}",
                coverage_plan_id=self.gesy_plan,
                gesy_referral_id=referral_id, copayment=copayment,
            ).status_code, 200)
        con = sqlite3.connect(self.db_path)
        values = con.execute("""
            SELECT pay.copayment FROM payments pay JOIN appointments a
              ON a.appointment_id=pay.appointment_id
            WHERE a.gesy_referral_id=? ORDER BY a.appointment_date
        """, (referral_id,)).fetchall()
        con.close()
        self.assertEqual(values, [(10.0,), (0.0,)])

    def test_gesy_rate_change_recalculates_charge_without_changing_financial_rows(self):
        referral_id = self.create_gesy_referral()
        created = self.new_session(
            1, appointment_date="2026-09-04", coverage_plan_id=self.gesy_plan,
            gesy_referral_id=referral_id, copayment=10,
        )
        appointment_id = created.get_json()["appointment_id"]
        con = sqlite3.connect(self.db_path)
        before = con.execute(
            "SELECT amount_due,copayment,amount_paid,receipt_amount FROM payments WHERE appointment_id=?",
            (appointment_id,),
        ).fetchone()
        con.close()
        changed = self.client.post(
            "/api/gesy-months", json={"year": 2026, "month": 9, "rate": 22},
            headers=self.api_headers(),
        )
        self.assertEqual(changed.status_code, 200)
        html = self.client.get("/current?history_id=1").get_data(as_text=True)
        self.assertIn("<span>22</span>", html)
        con = sqlite3.connect(self.db_path)
        after = con.execute(
            "SELECT amount_due,copayment,amount_paid,receipt_amount FROM payments WHERE appointment_id=?",
            (appointment_id,),
        ).fetchone()
        con.close()
        self.assertEqual(before, after)

    def test_default_charge_change_keeps_old_non_gesy_snapshot(self):
        old = self.new_session(1, appointment_date="2026-09-05")
        self.assertEqual(old.get_json()["amount_due"], 35.0)
        self.client.post(
            f"/api/coverage-plans/{self.self_standard_plan}", json={
                "name": "Αυτοπληρωμή", "default_charge": 40, "active": True,
            }, headers=self.api_headers(),
        )
        new = self.new_session(1, appointment_date="2026-09-06")
        self.assertEqual(new.get_json()["amount_due"], 40.0)
        con = sqlite3.connect(self.db_path)
        values = con.execute("""
            SELECT pay.amount_due FROM payments pay JOIN appointments a
              ON a.appointment_id=pay.appointment_id
            WHERE a.appointment_date IN ('2026-09-05','2026-09-06')
            ORDER BY a.appointment_date
        """).fetchall()
        con.close()
        self.assertEqual(values, [(35.0,), (40.0,)])

    def test_invalid_coverage_referral_combinations_are_rejected(self):
        blank_referral = self.client.post(
            "/api/histories/1/gesy-referrals",
            json={"referral_number": "", "allowed_visits": 6},
            headers=self.api_headers(),
        )
        self.assertEqual(blank_referral.status_code, 400)
        referral_id = self.create_gesy_referral()
        self.assertEqual(self.new_session(
            1, appointment_date="2026-09-08", coverage_plan_id=self.gesy_plan,
            copayment=10,
        ).status_code, 400)
        self.assertEqual(self.new_session(
            1, appointment_date="2026-09-08",
            gesy_referral_id=referral_id,
        ).status_code, 400)
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,is_active,today) VALUES(2,2,1,0)"
        )
        con.commit(); con.close()
        self.assertEqual(self.new_session(
            2, appointment_date="2026-09-08", coverage_plan_id=self.gesy_plan,
            gesy_referral_id=referral_id, copayment=10,
        ).status_code, 400)

    def test_automatic_gesy_months_start_at_september_and_manual_older_month_is_allowed(self):
        referral_id = self.create_gesy_referral()
        blocked = self.new_session(
            1, appointment_date="2026-08-20", coverage_plan_id=self.gesy_plan,
            gesy_referral_id=referral_id, copayment=10,
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(self.client.post(
            "/api/gesy-months", json={"year": 2026, "month": 8, "rate": 24},
            headers=self.api_headers(),
        ).status_code, 200)
        self.assertEqual(self.new_session(
            1, appointment_date="2026-08-20", coverage_plan_id=self.gesy_plan,
            gesy_referral_id=referral_id, copayment=10,
        ).status_code, 200)
        self.assertEqual(self.new_session(
            1, appointment_date="2026-12-20", coverage_plan_id=self.gesy_plan,
            gesy_referral_id=referral_id, copayment=0,
        ).status_code, 200)
        con = sqlite3.connect(self.db_path)
        months = con.execute(
            "SELECT year,month,rate FROM GesyMonth ORDER BY year,month"
        ).fetchall()
        con.close()
        self.assertEqual(months, [
            (2026, 8, 24.0), (2026, 9, 26.0), (2026, 10, 26.0),
            (2026, 11, 26.0), (2026, 12, 26.0),
        ])

    def test_legacy_rows_keep_unknown_coverage_and_financial_values(self):
        con = sqlite3.connect(self.db_path)
        appointment = con.execute(
            "SELECT coverage_plan_id,gesy_referral_id FROM appointments WHERE appointment_id=1"
        ).fetchone()
        payment = con.execute(
            "SELECT amount_due,amount_paid,receipt_amount,copayment FROM payments WHERE appointment_id=1"
        ).fetchone()
        con.close()
        self.assertEqual(appointment, (None, None))
        self.assertEqual(payment, (35.0, 0.0, 0.0, None))


class ReceiptAmountMigrationTests(unittest.TestCase):
    def test_session_coverage_migration_is_additive_idempotent_and_conservative(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "legacy.db"
            create_sample_db(db_path)
            con = sqlite3.connect(db_path)
            con.execute(
                "UPDATE clinical_histories SET social_security='ΓΕΣΥ',gesy_referral='LEG-77' "
                "WHERE history_id=1"
            )
            con.execute(
                "UPDATE payments SET amount_due=10,amount_paid=7,receipt_amount=5 "
                "WHERE appointment_id=1"
            )
            con.commit(); con.close()
            config = {
                "TESTING": True, "DB_PATH": str(db_path),
                "META_DB_PATH": str(root / "meta.db"),
                "BACKUP_DIR": str(root / "backups"),
                "DATABASE_SELECTION_PATH": str(root / "selection.json"),
                "AUTO_BACKUP": False,
            }
            app = create_app(config)
            con = sqlite3.connect(db_path)
            self.assertEqual(con.execute(
                "SELECT coverage_plan_id,gesy_referral_id FROM appointments WHERE appointment_id=1"
            ).fetchone(), (None, None))
            self.assertEqual(con.execute(
                "SELECT amount_due,amount_paid,receipt_amount,copayment FROM payments WHERE appointment_id=1"
            ).fetchone(), (10.0, 7.0, 5.0, None))
            self.assertEqual(con.execute(
                "SELECT history_id,referral_number,allowed_visits FROM GesyReferrals"
            ).fetchall(), [(1, "LEG-77", None)])
            self.assertEqual(con.execute(
                "SELECT year,month,rate FROM GesyMonth"
            ).fetchall(), [(2026, 9, 26.0)])
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_key='2026_09_session_coverage_v1'"
            ).fetchone()[0], 1)
            con.close()
            backup_count = len(list((root / "backups").glob("*.db")))
            self.assertFalse(migrate_session_coverage(app))
            self.assertEqual(len(list((root / "backups").glob("*.db"))), backup_count)
            con = sqlite3.connect(db_path)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM GesyReferrals").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
            con.close()

    def test_future_appointments_migration_preserves_existing_data_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "clinic.db"
            meta_path = root / "meta.db"
            backup_dir = root / "backups"
            create_sample_db(db_path)
            config = {
                "TESTING": True,
                "DB_PATH": str(db_path),
                "META_DB_PATH": str(meta_path),
                "BACKUP_DIR": str(backup_dir),
                "AUTO_BACKUP": False,
            }
            app = create_app(config)
            con = sqlite3.connect(db_path)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 1)
            columns = {
                row[1] for row in con.execute("PRAGMA table_info('Future_appointments')")
            }
            indexes = {
                row[1] for row in con.execute("PRAGMA index_list('Future_appointments')")
            }
            foreign_keys = {
                (row[2], row[3], row[4])
                for row in con.execute("PRAGMA foreign_key_list('Future_appointments')")
            }
            con.close()
            self.assertTrue({
                "future_appointment_id", "patient_id", "history_id",
                "appointment_date", "start_time", "duration_minutes", "status",
                "notes", "completed_appointment_id", "created_at", "updated_at",
            }.issubset(columns))
            self.assertTrue({
                "idx_future_appointments_date",
                "idx_future_appointments_patient_date",
                "idx_future_appointments_history",
                "idx_future_appointments_status",
            }.issubset(indexes))
            self.assertIn(("patients", "patient_id", "patient_id"), foreign_keys)
            self.assertIn(("clinical_histories", "history_id", "history_id"), foreign_keys)
            backup_count = len(list(backup_dir.glob("clinic_*.db")))
            self.assertGreaterEqual(backup_count, 1)
            self.assertFalse(migrate_future_appointments(app))
            self.assertEqual(len(list(backup_dir.glob("clinic_*.db"))), backup_count)

    def test_legacy_receipt_column_is_renamed_preserved_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "legacy.db"
            meta_path = root / "meta.db"
            backup_dir = root / "backups"
            con = sqlite3.connect(db_path)
            con.executescript("""
                CREATE TABLE payments (
                    payment_id INTEGER PRIMARY KEY,
                    appointment_id INTEGER NOT NULL,
                    amount_due REAL DEFAULT 0,
                    amount_paid REAL DEFAULT 0,
                    receipt_number TEXT
                );
                INSERT INTO payments(
                    payment_id, appointment_id, amount_due, amount_paid, receipt_number
                ) VALUES(7, 11, 40.0, 40.0, 20.0);
            """)
            con.commit()
            con.close()

            config = {
                "TESTING": True,
                "DB_PATH": str(db_path),
                "META_DB_PATH": str(meta_path),
                "BACKUP_DIR": str(backup_dir),
                "AUTO_BACKUP": False,
            }
            first_app = create_app(config)

            con = sqlite3.connect(db_path)
            columns = [row[1] for row in con.execute("PRAGMA table_info(payments)")]
            migrated = con.execute(
                "SELECT payment_id, appointment_id, amount_due, amount_paid, receipt_amount "
                "FROM payments WHERE payment_id=7"
            ).fetchone()
            con.close()
            self.assertIn("receipt_amount", columns)
            self.assertNotIn("receipt_number", columns)
            self.assertEqual(migrated, (7, 11, 40.0, 40.0, "20.0"))
            backup_count = len(list(backup_dir.glob("legacy_*.db")))
            self.assertGreaterEqual(backup_count, 1)

            second_app = create_app(config)
            self.assertFalse(migrate_receipt_amount(second_app))
            self.assertEqual(len(list(backup_dir.glob("legacy_*.db"))), backup_count)

            con = sqlite3.connect(db_path)
            self.assertEqual(
                con.execute(
                    "SELECT payment_id, appointment_id, receipt_amount FROM payments"
                ).fetchall(),
                [(7, 11, "20.0")],
            )
            con.close()
            self.assertIsNotNone(first_app)


if __name__ == "__main__":
    unittest.main()
