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


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "data" / "physio_new.db"
DEFAULT_META_DB = BASE_DIR / "data" / "physio_app_meta.db"

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
    "appointments": {
        "appointment_number", "appointment_date", "appointment_time", "status", "notes", "today",
    },
    "payments": {
        "payment_date", "amount_due", "amount_paid", "receipt_amount", "payment_method", "notes",
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
def db_conn(app: Any) -> Iterator[sqlite3.Connection]:
    con = connect_db(app.config["DB_PATH"])
    try:
        yield con
    finally:
        con.close()


@contextmanager
def write_transaction(app: Any, *, with_meta: bool = True) -> Iterator[sqlite3.Connection]:
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
            if set(extra) != {"appointments", "payments"}:
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
                ("appointments", "appointment_id"),
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
