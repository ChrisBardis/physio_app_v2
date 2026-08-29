from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import create_app


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
    receipt_number TEXT, payment_method TEXT, notes TEXT,
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
    con.execute("INSERT INTO payments(appointment_id,payment_date,amount_due,amount_paid,receipt_number) VALUES(1,'2026-08-21',35,0,'0')")
    con.commit()
    con.close()


class PhysioAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "clinic.db"
        self.meta_path = self.root / "meta.db"
        self.backup_dir = self.root / "backups"
        create_sample_db(self.db_path)
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": str(self.db_path),
            "META_DB_PATH": str(self.meta_path),
            "BACKUP_DIR": str(self.backup_dir),
            "AUTO_BACKUP": False,
        })
        self.client = self.app.test_client()
        self.client.get("/")

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
            "INSERT INTO payments(payment_id,appointment_id,payment_date,amount_due,amount_paid,receipt_number) "
            "VALUES(2,2,'2026-08-23',35,10,'2')"
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
            {key: response.get_json()[key] for key in ("histories", "appointments", "payments")},
            {"histories": 1, "appointments": 1, "payments": 1},
        )
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM patients WHERE patient_id=2").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM clinical_histories WHERE patient_id=2").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE history_id=2").fetchone()[0], 0)
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
        self.assertEqual(con.execute("SELECT COUNT(*) FROM payments WHERE appointment_id=2").fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()

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
        appointment = self.client.post(
            f"/api/appointment/new/{history_id}", json={}, headers=self.api_headers(),
        )
        self.assertEqual(appointment.status_code, 200)
        body = appointment.get_json()
        self.assertGreater(body["appointment_id"], 0)
        self.assertGreater(body["payment_id"], 0)

    def test_new_appointment_rejects_a_second_presence_on_the_same_day(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO clinical_histories(history_id,patient_id,history_date,is_active,today) "
            "VALUES(2,2,'2026-08-22',1,1)"
        )
        con.commit()
        con.close()

        first = self.client.post(
            "/api/appointment/new/2", json={}, headers=self.api_headers(),
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            "/api/appointment/new/2", json={}, headers=self.api_headers(),
        )
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
        response = self.client.post(
            "/api/appointments/1/delete", json={}, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.count("payments"), 0)
        self.assertTrue(list(self.backup_dir.glob("*.db")))

        undo = self.client.post("/api/undo", json={}, headers=self.api_headers())
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.count("payments"), 1)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()

    def test_new_appointment_uses_previous_charge(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE clinical_histories SET social_security='ΓΕΣΥ' WHERE history_id=1")
        con.execute("UPDATE payments SET amount_due=27.5 WHERE appointment_id=1")
        con.commit()
        con.close()

        response = self.client.post(
            "/api/appointment/new/1", json={}, headers=self.api_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["amount_due"], 27.5)
        con = sqlite3.connect(self.db_path)
        self.assertEqual(
            con.execute(
                "SELECT amount_due FROM payments WHERE payment_id=?",
                (response.get_json()["payment_id"],),
            ).fetchone()[0],
            27.5,
        )
        con.close()

    def test_first_appointment_charge_depends_on_social_security(self):
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

        expected = {2: 10.0, 3: 35.0, 4: 35.0}
        for history_id, amount_due in expected.items():
            with self.subTest(history_id=history_id):
                response = self.client.post(
                    f"/api/appointment/new/{history_id}", json={}, headers=self.api_headers(),
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["amount_due"], amount_due)

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

    def test_settings_explains_automatic_session_charge_rule(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Χρησιμοποιείται η χρέωση της προηγούμενης συνεδρίας", html)
        self.assertIn("10,00 € για ΓΕΣΥ", html)
        self.assertIn("35,00 €", html)
        self.assertNotIn('id="default-amount"', html)

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
            "ΕΝΕΡΓΑ ΙΣΤΟΡΙΚΑ", "ΕΝΕΡΓΟΠΟΙΗΣΗ", "ΣΗΜΕΡΙΝΑ ΙΣΤΟΡΙΚΑ", "ΣΗΜΕΡΑ",
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

    def test_new_forms_have_all_nine_autocomplete_fields(self):
        patient_html = self.client.get("/patients/new").get_data(as_text=True)
        patient_detail_html = self.client.get("/patients/1").get_data(as_text=True)
        history_html = self.client.get("/histories/new?patient_id=1").get_data(as_text=True)
        history_detail_html = self.client.get("/histories/1").get_data(as_text=True)
        patient_fields = ("first_name", "city", "referral", "profession")
        history_fields = (
            "main_diagnosis", "body_area", "social_security", "doctor", "icd10_code",
        )
        for field in patient_fields:
            self.assertIn(f'data-autocomplete="{field}"', patient_html)
            self.assertIn(f'data-autocomplete="{field}"', patient_detail_html)
        self.assertEqual(patient_detail_html.count('class="autocomplete-toggle"'), 4)
        self.assertIn('data-autocomplete-create="profession"', patient_detail_html)
        self.assertNotIn('autocomplete="off"', patient_detail_html)
        self.assertIn('app.js?v=20260829-today-attendance-controls', patient_detail_html)
        self.assertNotIn('autocomplete="family-name"', patient_html)
        self.assertNotIn('autocomplete="street-address"', patient_html)
        self.assertNotIn('autocomplete="tel"', patient_html)
        self.assertNotIn('autocomplete="email"', patient_html)
        self.assertIn('autocomplete="off"', patient_html)
        for field in history_fields:
            self.assertIn(f'data-autocomplete="{field}"', history_html)
        for field in ("main_diagnosis", "body_area", "social_security"):
            self.assertIn(f'data-autocomplete="{field}"', history_detail_html)
        self.assertEqual(history_detail_html.count('class="autocomplete-toggle"'), 3)

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
            descending.index('href="/patients/1"'),
            descending.index('href="/patients/2"'),
        )

        ascending = self.client.get(
            "/patients?sort=history_count&dir=asc"
        ).get_data(as_text=True)
        self.assertLess(
            ascending.index('href="/patients/2"'),
            ascending.index('href="/patients/1"'),
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
        self.assertLess(header.index("Ενέργεια"), header.index("Κινητό"))
        self.assertLess(header.index("Κινητό"), header.index("Αριθμός Ταυτότητας"))
        self.assertLess(header.index("Αριθμός Ταυτότητας"), header.index("Γέννηση"))

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
            "INSERT INTO payments(appointment_id,payment_date,amount_due,amount_paid,receipt_number) VALUES(2,'2026-08-22',15.5,10,'5')"
        )
        con.commit()
        con.close()

        html = self.client.get("/current?history_id=1").get_data(as_text=True)
        toolbar = html.split('<div class="session-toolbar">', 1)[1].split("</div>\n    <div class=\"table-wrap", 1)[0]
        self.assertLess(toolbar.index("Συνεδρίες"), toolbar.index('class="session-totals"'))
        self.assertLess(toolbar.index('class="session-totals"'), toolbar.index('id="new-appointment"'))
        self.assertIn('<span>Χρέωση</span><strong data-session-total="due">50.50</strong>', toolbar)
        self.assertIn('<span>Πίστωση</span><strong data-session-total="credit">10.00</strong>', toolbar)
        self.assertIn('<span>Αποδείξεις</span><strong data-session-total="receipts">5.00</strong>', toolbar)
        self.assertNotIn("Σύνολο Χρέωσης", toolbar)

    def test_current_filters_daily_active_histories_active_patients_and_all(self):
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

        default_html = self.client.get("/current").get_data(as_text=True)
        for key, label in (
            ("today", "Ημερήσια"),
            ("active_histories", "Ενεργά ιστορικά"),
            ("active_patients", "Ενεργός ασθενής"),
            ("all", "Όλες οι εγγραφές"),
        ):
            self.assertIn(f'data-current-filter="{key}"', default_html)
            self.assertIn(label, default_html)
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

    def test_topbar_has_requested_navigation_order(self):
        html = self.client.get("/patients").get_data(as_text=True)
        nav = html.split('<nav id="main-nav">', 1)[1].split("</nav>", 1)[0]
        labels = ["Ασθενείς", "Ιστορικά", "Ενεργοποίηση", "Ημερήσια", "Σήμερα", "Ρυθμίσεις", "Αναίρεση"]
        positions = [nav.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Ενεργά ιστορικά", nav)

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
            "Κοινωνική ασφάλιση", "Παραπεμπτικό ΓεΣΥ", "Γέννηση",
            "Αριθμός ταυτότητας", "Κινητό",
        ]
        positions = [header.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(html.count('href="/histories/2"'), 1)
        self.assertIn('data-table="clinical_histories" data-pk="2" data-column="social_security"', html)
        self.assertIn('data-table="patients" data-pk="2" data-column="mobile_phone"', html)
        self.assertIn('name="date" class="date-input" inputmode="numeric" placeholder="DD/MM/YYYY" value="21/08/2026"', html)
        self.assertIn('id="today-native-date" type="date" value="2026-08-21"', html)
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

    def test_manual_backup(self):
        response = self.client.post("/api/backup", json={}, headers=self.api_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(self.backup_dir.glob("*.db"))), 1)

    def test_security_headers_and_csrf(self):
        response = self.client.get("/patients")
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        rejected = self.client.post("/api/update", json={})
        self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
