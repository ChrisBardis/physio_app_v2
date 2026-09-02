from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from fysio_paths import (
    ARCHIVE_DIR,
    DEFAULT_BACKUP_DIR,
    DEFAULT_DATABASE_SELECTION,
    DEFAULT_DB,
    DEFAULT_META_DB,
    LOG_DIR,
    RESOURCE_ROOT,
    STATIC_DIR,
    TEMPLATE_DIR,
    prepare_writable_layout,
)


BASE_DIR = RESOURCE_ROOT

REQUIRED_CLINICAL_TABLES = {
    "patients",
    "clinical_histories",
    "appointments",
    "payments",
    "referrals",
    "professions",
    "doctors",
}

REQUIRED_CLINICAL_COLUMNS = {
    "patients": {"patient_id", "first_name", "last_name", "is_active"},
    "clinical_histories": {"history_id", "patient_id", "is_active", "today"},
    "appointments": {"appointment_id", "history_id", "appointment_date"},
    "payments": {"payment_id", "appointment_id", "amount_due", "amount_paid"},
}

ALLOWED_COLUMNS = {
    "patients": {
        "first_name", "last_name", "gender", "mobile_phone", "home_phone", "work_phone",
        "email", "birthdate", "identity_number", "address", "city", "postal_code",
        "referral_id", "profession_id", "notes", "is_active",
    },
    "clinical_histories": {
        "history_date", "problem_description", "main_diagnosis", "date_completed", "is_active",
        "doctor_id", "social_security", "body_area", "for_print", "for_xrays", "for_exercise",
        "today", "icd10_code", "gesy_referral",
    },
    "payments": {
        "payment_date", "amount_due", "amount_paid", "receipt_amount", "copayment",
        "payment_method", "notes",
    },
    "appointments": {
        "appointment_number", "appointment_date", "appointment_time", "notes", "today",
    },
}

PK_COLUMNS = {
    "patients": "patient_id",
    "clinical_histories": "history_id",
    "appointments": "appointment_id",
    "payments": "payment_id",
}

BOOL_COLUMNS = {
    ("patients", "is_active"),
    ("clinical_histories", "is_active"),
    ("clinical_histories", "for_print"),
    ("clinical_histories", "today"),
    ("appointments", "today"),
}

POSITIVE_INT_COLUMNS = {
    ("patients", "referral_id"),
    ("patients", "profession_id"),
    ("clinical_histories", "doctor_id"),
    ("appointments", "appointment_number"),
}

MONEY_COLUMNS = {
    ("payments", "amount_due"),
    ("payments", "amount_paid"),
    ("payments", "receipt_amount"),
    ("payments", "copayment"),
}

DATE_COLUMNS = {
    ("patients", "birthdate"),
    ("clinical_histories", "history_date"),
    ("clinical_histories", "date_completed"),
    ("appointments", "appointment_date"),
    ("payments", "payment_date"),
}


class ValidationError(ValueError):
    pass


def validate_clinical_database(db_path: str | Path) -> Path:
    """Return a verified, existing clinical SQLite database path.

    The connection is strictly read-only so a missing or invalid selection can
    never result in SQLite silently creating a replacement file.
    """
    path = Path(db_path).expanduser().resolve()
    if path.suffix.casefold() != ".db":
        raise ValidationError("Επιλέξτε αρχείο βάσης με κατάληξη .db")
    if not path.exists() or not path.is_file():
        raise ValidationError(f"Η βάση δεν βρέθηκε: {path}")

    try:
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10,
        )
    except sqlite3.Error as exc:
        raise ValidationError(f"Το αρχείο δεν είναι έγκυρη βάση SQLite: {exc}") from exc
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            detail = integrity[0] if integrity else "άγνωστο σφάλμα"
            raise ValidationError(f"Η βάση απέτυχε στον έλεγχο ακεραιότητας: {detail}")
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_tables = sorted(REQUIRED_CLINICAL_TABLES - tables)
        if missing_tables:
            raise ValidationError(
                "Η βάση δεν έχει το αναμενόμενο schema. Λείπουν οι πίνακες: "
                + ", ".join(missing_tables)
            )
        for table, required_columns in REQUIRED_CLINICAL_COLUMNS.items():
            columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                raise ValidationError(
                    f"Η βάση δεν έχει το αναμενόμενο schema στον πίνακα {table}. "
                    "Λείπουν τα πεδία: " + ", ".join(missing_columns)
                )
    except sqlite3.Error as exc:
        raise ValidationError(f"Δεν ήταν δυνατός ο έλεγχος της βάσης: {exc}") from exc
    finally:
        con.close()

    return path


def load_database_selection(config_path: str | Path) -> Path | None:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Δεν διαβάστηκε η αποθηκευμένη επιλογή βάσης: {exc}") from exc
    selected = payload.get("database_path") if isinstance(payload, dict) else None
    if not isinstance(selected, str) or not selected.strip():
        raise ValidationError("Η αποθηκευμένη επιλογή βάσης δεν είναι έγκυρη")
    return Path(selected).expanduser().resolve()


def save_database_selection(config_path: str | Path, db_path: str | Path) -> None:
    config = Path(config_path).expanduser().resolve()
    selected = Path(db_path).expanduser().resolve()
    config.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.with_name(f".{config.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {"database_path": str(selected)}, ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)


def choose_database_file(
    initial_directory: str | Path, *, create_new: bool = False,
) -> Path | None:
    """Open the native Windows file dialog used by the local application."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except (ImportError, RuntimeError) as exc:
        raise ValidationError(f"Δεν είναι διαθέσιμο το παράθυρο επιλογής των Windows: {exc}") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        common = {
            "initialdir": str(Path(initial_directory).expanduser().resolve()),
            "filetypes": [("Βάσεις SQLite", "*.db"), ("Όλα τα αρχεία", "*.*")],
            "parent": root,
        }
        if create_new:
            selected = filedialog.asksaveasfilename(
                title="Δημιουργία κενής βάσης εργασίας",
                defaultextension=".db",
                **common,
            )
        else:
            selected = filedialog.askopenfilename(
                title="Επιλογή βάσης εργασίας",
                **common,
            )
    finally:
        root.destroy()
    return Path(selected).expanduser().resolve() if selected else None


def create_empty_schema_database(source_path: str | Path, destination_path: str | Path) -> Path:
    """Create a data-free database containing the source clinical schema."""
    source_path = validate_clinical_database(source_path)
    destination = Path(destination_path).expanduser().resolve()
    if destination.suffix.casefold() != ".db":
        destination = destination.with_suffix(".db")
    if destination == source_path:
        raise ValidationError("Η νέα βάση δεν μπορεί να αντικαταστήσει τη βάση εργασίας")
    if destination.exists():
        raise ValidationError("Υπάρχει ήδη αρχείο με αυτό το όνομα")
    if not destination.parent.exists():
        raise ValidationError("Ο φάκελος προορισμού δεν υπάρχει")

    source = sqlite3.connect(
        f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30,
    )
    target = sqlite3.connect(destination, timeout=30)
    try:
        schema_rows = source.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
            ORDER BY CASE type
                WHEN 'table' THEN 1 WHEN 'index' THEN 2
                WHEN 'trigger' THEN 3 WHEN 'view' THEN 4 ELSE 5 END,
                name
            """
        ).fetchall()
        if not schema_rows:
            raise ValidationError("Η βάση εργασίας δεν περιέχει schema")
        target.execute("BEGIN IMMEDIATE")
        for _object_type, _name, sql in schema_rows:
            target.execute(sql)
        user_version = source.execute("PRAGMA user_version").fetchone()[0]
        application_id = source.execute("PRAGMA application_id").fetchone()[0]
        target.execute(f"PRAGMA user_version={int(user_version)}")
        target.execute(f"PRAGMA application_id={int(application_id)}")
        target.commit()
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValidationError(f"Η νέα βάση απέτυχε στον έλεγχο ακεραιότητας: {integrity}")
        for table in REQUIRED_CLINICAL_TABLES:
            if target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != 0:
                raise ValidationError("Η νέα βάση περιέχει δεδομένα")
    except Exception:
        target.rollback()
        target.close()
        source.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
    validate_clinical_database(destination)
    return destination


def normalize_search_text(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or "").casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def database_identity(db_path: str | Path) -> str:
    resolved = str(Path(db_path).expanduser().resolve()).casefold()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def default_meta_path(db_path: str | Path) -> Path:
    db = Path(db_path).expanduser().resolve()
    if db == DEFAULT_DB.resolve():
        return DEFAULT_META_DB
    return db.with_name(f"{db.stem}_app_meta.db")


def connect_db(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.create_function("PYCASEFOLD", 1, normalize_search_text, deterministic=True)
    return con


@contextmanager
def database_guard(app: Any) -> Iterator[None]:
    lock = app.config.get("DB_SWITCH_LOCK")
    if lock is None:
        yield
        return
    with lock:
        yield


@contextmanager
def db_conn(app: Any) -> Iterator[sqlite3.Connection]:
    with database_guard(app):
        con = connect_db(app.config["DB_PATH"])
        try:
            yield con
        finally:
            con.close()


@contextmanager
def write_transaction(app: Any, *, with_meta: bool = True) -> Iterator[sqlite3.Connection]:
    with database_guard(app):
        con = connect_db(app.config["DB_PATH"])
        try:
            if with_meta:
                con.execute("ATTACH DATABASE ? AS app_meta", (str(app.config["META_DB_PATH"]),))
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def init_meta_db(meta_path: str | Path) -> None:
    path = Path(meta_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                operation TEXT NOT NULL,
                table_name TEXT NOT NULL,
                pk_name TEXT NOT NULL,
                pk_value TEXT NOT NULL,
                column_name TEXT,
                old_value TEXT,
                new_value TEXT,
                extra_json TEXT,
                undone INTEGER NOT NULL DEFAULT 0,
                db_identity TEXT,
                description TEXT
            );
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(change_log)")}
        if "db_identity" not in columns:
            con.execute("ALTER TABLE change_log ADD COLUMN db_identity TEXT")
        if "description" not in columns:
            con.execute("ALTER TABLE change_log ADD COLUMN description TEXT")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_change_log_db_undo ON change_log(db_identity, undone, id)"
        )
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('default_amount_due','35.00')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('first_gesy_amount','10.00')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('first_other_amount','35.00')")
        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('secret_key',?)",
            (secrets.token_hex(32),),
        )
        con.commit()
    finally:
        con.close()


def migrate_receipt_amount(app: Any) -> bool:
    """Rename the legacy payments receipt column after a verified backup.

    Returns True only when this invocation performed the migration. If the new
    column already exists, no schema change is made, including when both names
    are present.
    """
    db_path = Path(app.config["DB_PATH"]).resolve()
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        table_exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payments'"
        ).fetchone()
        if not table_exists:
            return False
        columns = {row[1] for row in con.execute("PRAGMA table_info(payments)")}
    finally:
        con.close()

    if "receipt_amount" in columns or "receipt_number" not in columns:
        return False

    create_backup(app, force=True)
    con = connect_db(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in con.execute("PRAGMA table_info(payments)")}
        if "receipt_amount" in columns or "receipt_number" not in columns:
            con.rollback()
            return False
        con.execute(
            'ALTER TABLE payments RENAME COLUMN receipt_number TO receipt_amount'
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def migrate_future_appointments(app: Any) -> bool:
    """Create or finish the scheduled-appointment schema after a backup."""
    db_path = Path(app.config["DB_PATH"]).resolve()
    required_indexes = {
        "idx_future_appointments_date",
        "idx_future_appointments_patient_date",
        "idx_future_appointments_history",
        "idx_future_appointments_status",
    }
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name)=lower(?)",
            ("Future_appointments",),
        ).fetchone()
        indexes = {
            row[1] for row in con.execute("PRAGMA index_list('Future_appointments')")
        } if exists else set()
    finally:
        con.close()
    if exists and required_indexes.issubset(indexes):
        return False
    create_backup(app, force=True)
    con = connect_db(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""
            CREATE TABLE IF NOT EXISTS Future_appointments (
                future_appointment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                history_id INTEGER NOT NULL,
                appointment_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(status IN ('scheduled','completed','cancelled','no_show')),
                notes TEXT,
                completed_appointment_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(patient_id) REFERENCES patients(patient_id),
                FOREIGN KEY(history_id) REFERENCES clinical_histories(history_id),
                FOREIGN KEY(completed_appointment_id) REFERENCES appointments(appointment_id)
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_future_appointments_date "
            "ON Future_appointments(appointment_date)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_future_appointments_patient_date "
            "ON Future_appointments(patient_id, appointment_date)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_future_appointments_history "
            "ON Future_appointments(history_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_future_appointments_status "
            "ON Future_appointments(status)"
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


SESSION_COVERAGE_MIGRATION = "2026_09_session_coverage_v1"


def migrate_session_coverage(app: Any) -> bool:
    """Add the per-session coverage model without inferring legacy coverage.

    Existing appointment financial values remain byte-for-byte untouched. The
    migration only creates legacy referral records from the history-level text;
    it deliberately leaves every existing appointment coverage/referral and
    payment copayment NULL.
    """
    db_path = Path(app.config["DB_PATH"]).resolve()
    read_only = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30,
    )
    try:
        tables = {
            row[0] for row in read_only.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"clinical_histories", "appointments", "payments", "Future_appointments"}.issubset(tables):
            return False
        already_done = bool(read_only.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() and read_only.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key=?",
            (SESSION_COVERAGE_MIGRATION,),
        ).fetchone())
    finally:
        read_only.close()
    if already_done:
        return False

    create_backup(app, force=True)
    con = connect_db(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_key TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                details TEXT
            )
        """)
        if con.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key=?",
            (SESSION_COVERAGE_MIGRATION,),
        ).fetchone():
            con.rollback()
            return False

        con.execute("""
            CREATE TABLE IF NOT EXISTS CoveragePlans (
                coverage_plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                coverage_type TEXT NOT NULL
                    CHECK(coverage_type IN ('GESY','PRIVATE_INSURANCE','SELF_PAY')),
                name TEXT NOT NULL,
                default_charge REAL CHECK(default_charge IS NULL OR default_charge >= 0),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS GesyReferrals (
                gesy_referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id INTEGER NOT NULL,
                referral_number TEXT,
                allowed_visits INTEGER CHECK(allowed_visits IS NULL OR allowed_visits > 0),
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(history_id) REFERENCES clinical_histories(history_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS GesyMonth (
                gesy_month_id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                rate REAL NOT NULL CHECK(rate >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(year, month)
            )
        """)

        appointment_columns = {
            row[1] for row in con.execute("PRAGMA table_info(appointments)")
        }
        if "coverage_plan_id" not in appointment_columns:
            con.execute(
                "ALTER TABLE appointments ADD COLUMN coverage_plan_id INTEGER "
                "REFERENCES CoveragePlans(coverage_plan_id)"
            )
        if "gesy_referral_id" not in appointment_columns:
            con.execute(
                "ALTER TABLE appointments ADD COLUMN gesy_referral_id INTEGER "
                "REFERENCES GesyReferrals(gesy_referral_id)"
            )

        future_columns = {
            row[1] for row in con.execute("PRAGMA table_info(Future_appointments)")
        }
        if "coverage_plan_id" not in future_columns:
            con.execute(
                "ALTER TABLE Future_appointments ADD COLUMN coverage_plan_id INTEGER "
                "REFERENCES CoveragePlans(coverage_plan_id)"
            )
        if "gesy_referral_id" not in future_columns:
            con.execute(
                "ALTER TABLE Future_appointments ADD COLUMN gesy_referral_id INTEGER "
                "REFERENCES GesyReferrals(gesy_referral_id)"
            )

        payment_columns = {row[1] for row in con.execute("PRAGMA table_info(payments)")}
        if "copayment" not in payment_columns:
            con.execute(
                "ALTER TABLE payments ADD COLUMN copayment REAL "
                "CHECK(copayment IS NULL OR copayment >= 0)"
            )

        duplicate_payment = con.execute("""
            SELECT appointment_id FROM payments
            GROUP BY appointment_id HAVING COUNT(*) > 1 LIMIT 1
        """).fetchone()
        if duplicate_payment:
            raise ValidationError(
                "Η migration ακυρώθηκε: υπάρχουν πολλαπλές οικονομικές εγγραφές "
                "για την ίδια συνεδρία"
            )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_appointment "
            "ON payments(appointment_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_appointments_coverage "
            "ON appointments(coverage_plan_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_appointments_gesy_referral "
            "ON appointments(gesy_referral_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_gesy_referrals_history "
            "ON GesyReferrals(history_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_future_appointments_coverage "
            "ON Future_appointments(coverage_plan_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_future_appointments_gesy_referral "
            "ON Future_appointments(gesy_referral_id)"
        )

        plans = (
            ("GESY", "GESY", "ΓεΣΥ", None),
            ("SELF_STANDARD", "SELF_PAY", "Αυτοπληρωμή", 35.0),
            ("SELF_DISCOUNT", "SELF_PAY", "Αυτοπληρωμή μειωμένη", 30.0),
            ("PRIVATE_STANDARD", "PRIVATE_INSURANCE", "Ιδιωτική ασφάλιση", 35.0),
            ("PRIVATE_DISCOUNT", "PRIVATE_INSURANCE", "Ιδιωτική ασφάλιση μειωμένη", 30.0),
        )
        con.executemany("""
            INSERT OR IGNORE INTO CoveragePlans(
                code, coverage_type, name, default_charge, active
            ) VALUES(?,?,?,?,1)
        """, plans)
        con.execute("""
            INSERT OR IGNORE INTO GesyMonth(year, month, rate)
            VALUES(2026, 9, 26.00)
        """)
        con.execute("""
            INSERT INTO GesyReferrals(
                history_id, referral_number, allowed_visits, notes
            )
            SELECT h.history_id, TRIM(h.gesy_referral), NULL,
                   'Legacy migration από clinical_histories.gesy_referral'
            FROM clinical_histories h
            WHERE TRIM(COALESCE(h.gesy_referral,'')) <> ''
        """)
        details = json.dumps({
            "legacy_coverage_assignment": "none",
            "legacy_copayment_assignment": "none",
            "gesy_seed": "2026-09:26.00",
        }, ensure_ascii=False, sort_keys=True)
        con.execute(
            "INSERT INTO schema_migrations(migration_key, details) VALUES(?,?)",
            (SESSION_COVERAGE_MIGRATION, details),
        )
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValidationError(
                f"Η migration δημιούργησε {len(violations)} παραβιάσεις foreign key"
            )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
def meta_conn(app: Any) -> sqlite3.Connection:
    con = sqlite3.connect(str(app.config["META_DB_PATH"]), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def get_setting(app: Any, key: str, default: str) -> str:
    con = meta_conn(app)
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        con.close()


def set_setting(app: Any, key: str, value: str) -> None:
    con = meta_conn(app)
    try:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


def log_change(
    con: sqlite3.Connection,
    app: Any,
    operation: str,
    table: str,
    pk_name: str,
    pk_value: Any,
    column: str | None,
    old: Any,
    new: Any,
    description: str,
) -> None:
    extra = new if isinstance(new, (dict, list)) else None
    new_scalar = None if extra is not None else new
    con.execute("""
        INSERT INTO app_meta.change_log(
            operation,table_name,pk_name,pk_value,column_name,old_value,new_value,
            extra_json,db_identity,description
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
    """, (
        operation,
        table,
        pk_name,
        str(pk_value),
        column,
        json.dumps(old, ensure_ascii=False),
        json.dumps(new_scalar, ensure_ascii=False),
        json.dumps(extra, ensure_ascii=False) if extra is not None else None,
        app.config["DB_IDENTITY"],
        description,
    ))


def get_last_change(app: Any) -> sqlite3.Row | None:
    con = meta_conn(app)
    try:
        return con.execute(
            "SELECT * FROM change_log WHERE db_identity=? AND undone=0 ORDER BY id DESC LIMIT 1",
            (app.config["DB_IDENTITY"],),
        ).fetchone()
    finally:
        con.close()


def undo_last_change(app: Any) -> str:
    with write_transaction(app, with_meta=True) as con:
        change = con.execute(
            "SELECT * FROM app_meta.change_log "
            "WHERE db_identity=? AND undone=0 ORDER BY id DESC LIMIT 1",
            (app.config["DB_IDENTITY"],),
        ).fetchone()
        if not change:
            raise LookupError("Δεν υπάρχει αλλαγή για αναίρεση")

        operation = change["operation"]
        table = change["table_name"]
        pk_name = change["pk_name"]
        pk_value = change["pk_value"]

        if table not in PK_COLUMNS or PK_COLUMNS[table] != pk_name:
            raise ValidationError("Μη ασφαλής παλιά εγγραφή Undo")

        if operation == "update":
            column = change["column_name"]
            if column not in ALLOWED_COLUMNS.get(table, set()):
                raise ValidationError("Μη ασφαλές πεδίο Undo")
            old = json.loads(change["old_value"])
            con.execute(
                f'UPDATE "{table}" SET "{column}"=?, updated_at=CURRENT_TIMESTAMP '
                f'WHERE "{pk_name}"=?',
                (old, pk_value),
            )
        elif operation == "update_status_cascade":
            column = change["column_name"]
            if (table, column) not in {
                ("patients", "is_active"),
                ("clinical_histories", "is_active"),
            }:
                raise ValidationError("Μη ασφαλής αλλαγή κατάστασης Undo")
            old = json.loads(change["old_value"])
            extra = json.loads(change["extra_json"] or "{}")
            con.execute(
                f'UPDATE "{table}" SET "{column}"=?, updated_at=CURRENT_TIMESTAMP '
                f'WHERE "{pk_name}"=?',
                (old, pk_value),
            )
            history_ids = extra.get("cleared_today_history_ids", [])
            if not isinstance(history_ids, list) or any(
                not isinstance(history_id, int) or history_id <= 0
                for history_id in history_ids
            ):
                raise ValidationError("Μη ασφαλή ιστορικά Undo")
            for history_id in history_ids:
                con.execute("""
                    UPDATE clinical_histories SET today=1, updated_at=CURRENT_TIMESTAMP
                    WHERE history_id=? AND is_active=1
                      AND EXISTS (
                          SELECT 1 FROM patients p
                          WHERE p.patient_id=clinical_histories.patient_id AND p.is_active=1
                      )
                """, (history_id,))
        elif operation == "update_related":
            column = change["column_name"]
            if column not in ALLOWED_COLUMNS.get(table, set()):
                raise ValidationError("Μη ασφαλές πεδίο Undo")
            old = json.loads(change["old_value"])
            extra = json.loads(change["extra_json"] or "{}")
            con.execute(
                f'UPDATE "{table}" SET "{column}"=?, updated_at=CURRENT_TIMESTAMP '
                f'WHERE "{pk_name}"=?',
                (old, pk_value),
            )
            related_allowlist = {
                ("referrals", "referral_id"),
                ("professions", "profession_id"),
                ("doctors", "doctor_id"),
            }
            for related in reversed(extra.get("created_related", [])):
                related_table = related.get("table")
                related_pk = related.get("pk_name")
                related_value = related.get("pk_value")
                if (related_table, related_pk) not in related_allowlist:
                    raise ValidationError("Μη ασφαλής συσχετισμένη εγγραφή Undo")
                con.execute(
                    f'DELETE FROM "{related_table}" WHERE "{related_pk}"=?',
                    (related_value,),
                )
        elif operation == "insert":
            con.execute(f'DELETE FROM "{table}" WHERE "{pk_name}"=?', (pk_value,))
            extra = json.loads(change["extra_json"] or "{}")
            related_allowlist = {
                ("referrals", "referral_id"),
                ("professions", "profession_id"),
                ("doctors", "doctor_id"),
            }
            for related in reversed(extra.get("created_related", [])):
                related_table = related.get("table")
                related_pk = related.get("pk_name")
                related_value = related.get("pk_value")
                if (related_table, related_pk) not in related_allowlist:
                    raise ValidationError("Μη ασφαλής συσχετισμένη εγγραφή Undo")
                con.execute(
                    f'DELETE FROM "{related_table}" WHERE "{related_pk}"=?',
                    (related_value,),
                )
        elif operation == "insert_appointment":
            extra = json.loads(change["extra_json"] or "{}")
            payment_id = extra.get("payment_id")
            if payment_id:
                con.execute("DELETE FROM payments WHERE payment_id=?", (payment_id,))
            con.execute("DELETE FROM appointments WHERE appointment_id=?", (pk_value,))
        elif operation == "delete_appointment":
            if table != "appointments" or pk_name != "appointment_id":
                raise ValidationError("Μη ασφαλής διαγραφή παρουσίας Undo")
            extra = json.loads(change["extra_json"] or "{}")
            if set(extra) != {"appointments", "payments", "future_appointment_links"}:
                raise ValidationError("Ελλιπές αντίγραφο διαγραφής παρουσίας")
            appointment_rows = extra.get("appointments")
            if (
                not isinstance(appointment_rows, list)
                or len(appointment_rows) != 1
                or str(appointment_rows[0].get("appointment_id")) != str(pk_value)
            ):
                raise ValidationError("Μη ασφαλή στοιχεία παρουσίας Undo")
            for restore_table, restore_pk in (
                ("appointments", "appointment_id"),
                ("payments", "payment_id"),
            ):
                rows = extra.get(restore_table)
                if not isinstance(rows, list):
                    raise ValidationError("Μη ασφαλείς εγγραφές παρουσίας Undo")
                schema_columns = {
                    row["name"]
                    for row in con.execute(f'PRAGMA table_info("{restore_table}")').fetchall()
                }
                if restore_pk not in schema_columns:
                    raise ValidationError("Μη ασφαλές σχήμα παρουσίας Undo")
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or restore_pk not in row
                        or not row
                        or not set(row).issubset(schema_columns)
                    ):
                        raise ValidationError("Μη ασφαλής εγγραφή παρουσίας Undo")
                    columns = list(row)
                    column_sql = ",".join(f'"{column}"' for column in columns)
                    placeholders = ",".join("?" for _ in columns)
                    con.execute(
                        f'INSERT INTO "{restore_table}"({column_sql}) VALUES({placeholders})',
                        [row[column] for column in columns],
                    )
            future_links = extra.get("future_appointment_links")
            if not isinstance(future_links, list) or any(
                not isinstance(link_id, int) for link_id in future_links
            ):
                raise ValidationError("Μη ασφαλείς συνδέσεις ραντεβού Undo")
            for link_id in future_links:
                con.execute(
                    "UPDATE Future_appointments SET completed_appointment_id=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE future_appointment_id=?",
                    (pk_value, link_id),
                )
        elif operation == "activate_history":
            extra = json.loads(change["extra_json"] or "{}")
            con.execute(
                "UPDATE clinical_histories SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE history_id=?",
                (extra.get("old_history_active", 0), extra.get("history_id")),
            )
            con.execute(
                "UPDATE patients SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE patient_id=?",
                (extra.get("old_patient_active", 0), extra.get("patient_id")),
            )
        elif operation == "delete_patient":
            if table != "patients" or pk_name != "patient_id":
                raise ValidationError("Μη ασφαλής διαγραφή ασθενή Undo")
            extra = json.loads(change["extra_json"] or "{}")
            restore_plan = (
                ("patients", "patient_id"),
                ("clinical_histories", "history_id"),
                ("GesyReferrals", "gesy_referral_id"),
                ("appointments", "appointment_id"),
                ("Future_appointments", "future_appointment_id"),
                ("payments", "payment_id"),
            )
            if set(extra) != {item[0] for item in restore_plan}:
                raise ValidationError("Ελλιπές αντίγραφο διαγραφής ασθενή")
            patient_rows = extra.get("patients")
            if (
                not isinstance(patient_rows, list)
                or len(patient_rows) != 1
                or str(patient_rows[0].get("patient_id")) != str(pk_value)
            ):
                raise ValidationError("Μη ασφαλή στοιχεία ασθενή Undo")

            for restore_table, restore_pk in restore_plan:
                rows = extra.get(restore_table)
                if not isinstance(rows, list):
                    raise ValidationError("Μη ασφαλείς εγγραφές Undo")
                schema_columns = {
                    row["name"]
                    for row in con.execute(f'PRAGMA table_info("{restore_table}")').fetchall()
                }
                if restore_pk not in schema_columns:
                    raise ValidationError("Μη ασφαλές σχήμα Undo")
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or restore_pk not in row
                        or not row
                        or not set(row).issubset(schema_columns)
                    ):
                        raise ValidationError("Μη ασφαλής εγγραφή Undo")
                    columns = list(row)
                    column_sql = ",".join(f'"{column}"' for column in columns)
                    placeholders = ",".join("?" for _ in columns)
                    con.execute(
                        f'INSERT INTO "{restore_table}"({column_sql}) VALUES({placeholders})',
                        [row[column] for column in columns],
                    )
        elif operation == "delete_history":
            if table != "clinical_histories" or pk_name != "history_id":
                raise ValidationError("Μη ασφαλής διαγραφή ιστορικού Undo")
            extra = json.loads(change["extra_json"] or "{}")
            restore_plan = (
                ("clinical_histories", "history_id"),
                ("GesyReferrals", "gesy_referral_id"),
                ("appointments", "appointment_id"),
                ("Future_appointments", "future_appointment_id"),
                ("payments", "payment_id"),
            )
            if set(extra) != {item[0] for item in restore_plan}:
                raise ValidationError("Ελλιπές αντίγραφο διαγραφής ιστορικού")
            history_rows = extra.get("clinical_histories")
            if (
                not isinstance(history_rows, list)
                or len(history_rows) != 1
                or str(history_rows[0].get("history_id")) != str(pk_value)
            ):
                raise ValidationError("Μη ασφαλή στοιχεία ιστορικού Undo")
            for restore_table, restore_pk in restore_plan:
                rows = extra.get(restore_table)
                if not isinstance(rows, list):
                    raise ValidationError("Μη ασφαλείς εγγραφές ιστορικού Undo")
                schema_columns = {
                    row["name"]
                    for row in con.execute(f'PRAGMA table_info("{restore_table}")').fetchall()
                }
                if restore_pk not in schema_columns:
                    raise ValidationError("Μη ασφαλές σχήμα ιστορικού Undo")
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or restore_pk not in row
                        or not row
                        or not set(row).issubset(schema_columns)
                    ):
                        raise ValidationError("Μη ασφαλής εγγραφή ιστορικού Undo")
                    columns = list(row)
                    column_sql = ",".join(f'"{column}"' for column in columns)
                    placeholders = ",".join("?" for _ in columns)
                    con.execute(
                        f'INSERT INTO "{restore_table}"({column_sql}) VALUES({placeholders})',
                        [row[column] for column in columns],
                    )
        else:
            raise ValidationError(f"Μη υποστηριζόμενη αναίρεση: {operation}")

        con.execute("UPDATE app_meta.change_log SET undone=1 WHERE id=?", (change["id"],))
        return change["description"] or "Τελευταία αλλαγή"


def normalize_value(table: str, column: str, value: Any) -> Any:
    if table not in ALLOWED_COLUMNS or column not in ALLOWED_COLUMNS[table]:
        raise ValidationError("Μη επιτρεπτό πεδίο")

    if value is None or (isinstance(value, str) and not value.strip()):
        return None

    if (table, column) in BOOL_COLUMNS:
        if value in (True, 1, "1", "true", "TRUE"):
            return 1
        if value in (False, 0, "0", "false", "FALSE"):
            return 0
        raise ValidationError("Η επιλογή πρέπει να είναι Ναι ή Όχι")

    if (table, column) in POSITIVE_INT_COLUMNS:
        try:
            number = int(str(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Απαιτείται έγκυρος ακέραιος αριθμός") from exc
        if number <= 0:
            raise ValidationError("Ο αριθμός πρέπει να είναι μεγαλύτερος από μηδέν")
        return number

    if (table, column) in MONEY_COLUMNS:
        try:
            number = float(str(value).replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Απαιτείται έγκυρο χρηματικό ποσό") from exc
        if not math.isfinite(number) or number < 0 or number > 1_000_000:
            raise ValidationError("Το ποσό πρέπει να είναι από 0 έως 1.000.000")
        return round(number, 2)

    if (table, column) in DATE_COLUMNS:
        text = str(value).strip()
        for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, pattern).date().isoformat()
            except ValueError:
                continue
        raise ValidationError("Η ημερομηνία δεν είναι έγκυρη")

    text = str(value).strip()
    if len(text) > 10_000:
        raise ValidationError("Το κείμενο είναι υπερβολικά μεγάλο")
    if table == "patients" and column == "email" and text and ("@" not in text or "." not in text.rsplit("@", 1)[-1]):
        raise ValidationError("Το email δεν είναι έγκυρο")
    return text


def equivalent(a: Any, b: Any) -> bool:
    if a is None and b in (None, ""):
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


def validate_default_amount(value: Any) -> str:
    normalized = normalize_value("payments", "amount_due", value)
    if normalized is None:
        raise ValidationError("Το προεπιλεγμένο ποσό είναι υποχρεωτικό")
    return f"{normalized:.2f}"


def create_backup(app: Any, *, force: bool = False) -> Path:
    with database_guard(app):
        source_path = Path(app.config["DB_PATH"]).resolve()
        backup_dir = Path(app.config["BACKUP_DIR"]).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        today_prefix = f"{source_path.stem}_{date.today():%Y%m%d}"
        existing = sorted(backup_dir.glob(f"{today_prefix}_*.db"))
        if existing and not force:
            return existing[-1]

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        destination = backup_dir / f"{source_path.stem}_{stamp}.db"
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(destination)
        try:
            integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Αποτυχία ελέγχου βάσης: {integrity}")
            source.backup(target)
            target.commit()
        except Exception:
            target.close()
            source.close()
            destination.unlink(missing_ok=True)
            raise
        else:
            target.close()
            source.close()

    keep = max(1, int(app.config.get("BACKUP_RETENTION", 30)))
    backups = sorted(backup_dir.glob(f"{source_path.stem}_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old_backup in backups[keep:]:
        old_backup.unlink(missing_ok=True)
    return destination


def backup_status(app: Any) -> dict[str, Any]:
    source = Path(app.config["DB_PATH"])
    backup_dir = Path(app.config["BACKUP_DIR"])
    backups = sorted(
        backup_dir.glob(f"{source.stem}_*.db") if backup_dir.exists() else [],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    latest = backups[0] if backups else None
    return {
        "directory": str(backup_dir),
        "count": len(backups),
        "latest": datetime.fromtimestamp(latest.stat().st_mtime).strftime("%d/%m/%Y %H:%M") if latest else None,
        "latest_name": latest.name if latest else None,
    }


def data_health(app: Any) -> dict[str, Any]:
    with db_conn(app) as con:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_issues = len(con.execute("PRAGMA foreign_key_check").fetchall())
        checks = {
            "blank_patients": "SELECT COUNT(*) FROM patients WHERE TRIM(COALESCE(first_name,''))='' AND TRIM(COALESCE(last_name,''))=''",
            "blank_histories": "SELECT COUNT(*) FROM clinical_histories WHERE TRIM(COALESCE(main_diagnosis,''))='' AND TRIM(COALESCE(problem_description,''))=''",
            "appointments_without_payment": "SELECT COUNT(*) FROM appointments a WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.appointment_id=a.appointment_id)",
            "appointments_multiple_payments": "SELECT COUNT(*) FROM (SELECT appointment_id FROM payments GROUP BY appointment_id HAVING COUNT(*)>1)",
        }
        result = {key: con.execute(sql).fetchone()[0] for key, sql in checks.items()}
    result.update({"integrity": integrity, "foreign_key_issues": foreign_key_issues})
    return result
