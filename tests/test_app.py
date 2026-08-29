from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
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

    def test_home_launcher_order_and_today_label(self):
        html = self.client.get("/").get_data(as_text=True)
        labels = [
            "ΝΕΟΣ ΑΣΘΕΝΗΣ", "ΑΣΘΕΝΕΙΣ", "ΝΕΟ ΙΣΤΟΡΙΚΟ",
            "ΕΝΕΡΓΑ ΙΣΤΟΡΙΚΑ", "ΕΝΕΡΓΟΠΟΙΗΣΗ", "ΣΗΜΕΡΙΝΑ ΙΣΤΟΡΙΚΑ",
        ]
        positions = [html.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        launcher_html = html[html.index('class="launcher-grid"'):html.index('class="keyboard-hint"')]
        self.assertNotIn(">ΑΥΤΟΣ<", launcher_html)

    def test_new_forms_have_all_nine_autocomplete_fields(self):
        patient_html = self.client.get("/patients/new").get_data(as_text=True)
        patient_detail_html = self.client.get("/patients/1").get_data(as_text=True)
        history_html = self.client.get("/histories/new?patient_id=1").get_data(as_text=True)
        patient_fields = ("first_name", "city", "referral", "profession")
        history_fields = (
            "main_diagnosis", "body_area", "social_security", "doctor", "icd10_code",
        )
        for field in patient_fields:
            self.assertIn(f'data-autocomplete="{field}"', patient_html)
            self.assertIn(f'data-autocomplete="{field}"', patient_detail_html)
        self.assertEqual(patient_detail_html.count('class="autocomplete-toggle"'), 4)
        self.assertNotIn('autocomplete="off"', patient_detail_html)
        self.assertIn('app.js?v=20260829-no-browser-autofill', patient_detail_html)
        self.assertNotIn('autocomplete="family-name"', patient_html)
        self.assertNotIn('autocomplete="street-address"', patient_html)
        self.assertNotIn('autocomplete="tel"', patient_html)
        self.assertNotIn('autocomplete="email"', patient_html)
        self.assertIn('autocomplete="off"', patient_html)
        for field in history_fields:
            self.assertIn(f'data-autocomplete="{field}"', history_html)

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
