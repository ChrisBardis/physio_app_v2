from __future__ import annotations

import math
import os
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from physio_core import (
    ALLOWED_COLUMNS,
    BASE_DIR,
    ARCHIVE_DIR,
    DEFAULT_DB,
    DEFAULT_BACKUP_DIR,
    DEFAULT_DATABASE_SELECTION,
    PK_COLUMNS,
    LOG_DIR,
    STATIC_DIR,
    TEMPLATE_DIR,
    ValidationError,
    backup_status,
    choose_database_file,
    create_backup,
    create_empty_schema_database,
    data_health,
    database_identity,
    db_conn,
    default_meta_path,
    equivalent,
    get_last_change,
    get_setting,
    init_meta_db,
    load_database_selection,
    log_change,
    migrate_receipt_amount,
    migrate_future_appointments,
    migrate_session_coverage,
    normalize_search_text,
    normalize_value,
    prepare_writable_layout,
    save_database_selection,
    set_setting,
    undo_last_change,
    validate_clinical_database,
    validate_default_amount,
    write_transaction,
)
from coverage_service import (
    coverage_plans,
    effective_charge_sql,
    ensure_gesy_month,
    financial_values_for_new_session,
    parse_positive_int,
    referral_rows,
    validate_coverage,
)
from statistics_service import build_statistics


PATIENT_FORM_COLUMNS = (
    "first_name", "last_name", "address", "city", "postal_code", "gender",
    "work_phone", "home_phone", "mobile_phone", "email", "birthdate",
    "identity_number", "referral_id", "profession_id", "notes", "is_active",
)

HISTORY_FORM_COLUMNS = (
    "history_date", "main_diagnosis", "problem_description", "body_area",
    "date_completed", "doctor_id", "social_security", "icd10_code",
    "gesy_referral", "is_active", "today",
)

FIELD_LABELS = {
    "first_name": "Όνομα", "last_name": "Επώνυμο", "mobile_phone": "Κινητό",
    "birthdate": "Ημερομηνία γέννησης", "is_active": "Ενεργό",
    "today": "Εμφάνιση στην Ημερήσια", "history_date": "Ημερομηνία ιστορικού",
    "main_diagnosis": "Κύρια διάγνωση", "problem_description": "Περιγραφή προβλήματος",
    "appointment_number": "Αριθμός συνεδρίας", "appointment_date": "Ημερομηνία συνεδρίας",
    "amount_due": "Χρέωση", "amount_paid": "Πληρωμή", "receipt_amount": "Απόδειξη",
    "notes": "Σημειώσεις",
}


def startup_database_path(selection_path: str | Path) -> Path:
    explicit = os.environ.get("PHYSIO_DB_PATH")
    if explicit:
        try:
            return validate_clinical_database(explicit)
        except ValidationError as exc:
            raise RuntimeError(f"Η βάση του PHYSIO_DB_PATH δεν είναι έγκυρη: {exc}") from exc

    selection_error: ValidationError | None = None
    try:
        remembered = load_database_selection(selection_path)
    except ValidationError as exc:
        remembered = None
        selection_error = exc
    candidate = remembered or DEFAULT_DB
    try:
        verified = validate_clinical_database(candidate)
    except ValidationError as exc:
        selection_error = exc
    else:
        return verified

    initial_directory = candidate.parent if candidate.parent.exists() else DEFAULT_DB.parent
    try:
        chosen = choose_database_file(initial_directory)
    except Exception as exc:
        detail = selection_error or exc
        raise RuntimeError(
            f"Δεν μπορεί να ανοίξει η εφαρμογή χωρίς έγκυρη βάση εργασίας: {detail}"
        ) from exc
    if chosen is None:
        raise RuntimeError(
            "Η εκκίνηση ακυρώθηκε επειδή δεν επιλέχθηκε έγκυρη βάση εργασίας"
        )
    verified = validate_clinical_database(chosen)
    save_database_selection(selection_path, verified)
    return verified


def _show_database_notice(app: Flask) -> bool:
    notice_token = app.config["STARTUP_NOTICE_TOKEN"]
    if session.get("startup_notice_token") == notice_token:
        return False
    session["startup_notice_token"] = notice_token
    return True

LIST_BATCH_SIZE = 75

AUTOCOMPLETE_SOURCES = {
    "first_name": {
        "from": "FROM patients p",
        "value": "p.first_name",
        "id": "NULL",
        "prefix": True,
    },
    "city": {
        "from": "FROM patients p",
        "value": "p.city",
        "id": "NULL",
    },
    "referral": {
        "from": "FROM patients p JOIN referrals r ON r.referral_id=p.referral_id",
        "value": "TRIM(COALESCE(r.last_name,'') || ' ' || COALESCE(r.first_name,''))",
        "id": "r.referral_id",
        "prefix": True,
        "prefix_parts": ("r.first_name", "r.last_name"),
    },
    "profession": {
        "from": "FROM patients p JOIN professions pr ON pr.profession_id=p.profession_id",
        "value": "pr.profession_name",
        "id": "pr.profession_id",
    },
    "main_diagnosis": {
        "from": "FROM clinical_histories h",
        "value": "h.main_diagnosis",
        "id": "NULL",
    },
    "body_area": {
        "from": "FROM clinical_histories h",
        "value": "h.body_area",
        "id": "NULL",
    },
    "social_security": {
        "from": "FROM clinical_histories h",
        "value": "h.social_security",
        "id": "NULL",
    },
    "doctor": {
        "from": "FROM clinical_histories h JOIN doctors d ON d.doctor_id=h.doctor_id",
        "value": "TRIM(COALESCE(d.last_name,'') || ' ' || COALESCE(d.first_name,''))",
        "id": "d.doctor_id",
        "prefix": True,
        "prefix_parts": ("d.first_name", "d.last_name"),
    },
    "icd10_code": {
        "from": "FROM clinical_histories h",
        "value": "h.icd10_code",
        "id": "NULL",
    },
}


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    if not test_config:
        prepare_writable_layout()
    app = Flask(
        __name__,
        template_folder=str(TEMPLATE_DIR),
        static_folder=str(STATIC_DIR),
    )
    selection_path = Path(
        os.environ.get("PHYSIO_DATABASE_SELECTION", str(DEFAULT_DATABASE_SELECTION))
    ).expanduser().resolve()
    if test_config and test_config.get("DB_PATH"):
        db_path = Path(test_config["DB_PATH"]).expanduser().resolve()
    else:
        db_path = startup_database_path(selection_path)
    meta_path = Path(os.environ.get("PHYSIO_META_PATH", str(default_meta_path(db_path)))).expanduser().resolve()
    backup_dir = Path(
        os.environ.get("PHYSIO_BACKUP_DIR", str(DEFAULT_BACKUP_DIR))
    ).expanduser().resolve()
    app.config.update(
        DB_PATH=str(db_path), META_DB_PATH=str(meta_path), BACKUP_DIR=str(backup_dir),
        ARCHIVE_DIR=str(ARCHIVE_DIR), LOG_DIR=str(LOG_DIR),
        DATABASE_SELECTION_PATH=str(selection_path), DB_SWITCH_LOCK=threading.RLock(),
        STARTUP_NOTICE_TOKEN=secrets.token_urlsafe(16),
        BACKUP_RETENTION=30, AUTO_BACKUP=True, SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=1_000_000,
    )
    if test_config:
        app.config.update(test_config)
    app.config["DB_PATH"] = str(Path(app.config["DB_PATH"]).resolve())
    app.config["META_DB_PATH"] = str(Path(app.config["META_DB_PATH"]).resolve())
    app.config["BACKUP_DIR"] = str(Path(app.config["BACKUP_DIR"]).resolve())
    app.config["DATABASE_SELECTION_PATH"] = str(
        Path(app.config["DATABASE_SELECTION_PATH"]).resolve()
    )
    app.config["DB_IDENTITY"] = database_identity(app.config["DB_PATH"])
    init_meta_db(app.config["META_DB_PATH"])
    app.secret_key = get_setting(app, "secret_key", secrets.token_hex(32))

    migrate_receipt_amount(app)
    migrate_future_appointments(app)
    migrate_session_coverage(app)

    if app.config.get("AUTO_BACKUP"):
        try:
            create_backup(app)
        except Exception:
            app.logger.exception("Δεν ήταν δυνατή η αυτόματη δημιουργία backup")

    @app.template_filter("grdate")
    def grdate(value: Any) -> str:
        if value in (None, ""):
            return ""
        text = str(value).strip()
        for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(text[:10], pattern).strftime("%d/%m/%Y")
            except ValueError:
                continue
        return text

    @app.template_filter("money")
    def money(value: Any) -> str:
        try:
            return f"{float(value or 0):.2f} €".replace(".", ",")
        except (TypeError, ValueError):
            return "0,00 €"

    @app.template_filter("moneyvalue")
    def moneyvalue(value: Any) -> str:
        try:
            number = float(str(value or 0).strip().replace(",", "."))
            return f"{number:.2f}" if math.isfinite(number) else ""
        except (TypeError, ValueError):
            return ""

    @app.template_filter("wholemoney")
    def wholemoney(value: Any) -> str:
        try:
            number = float(str(value or 0).strip().replace(",", "."))
            return str(int(math.floor(number + 0.5))) if math.isfinite(number) else "0"
        except (TypeError, ValueError):
            return "0"

    @app.before_request
    def csrf_protection():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not supplied or not secrets.compare_digest(supplied, session["_csrf_token"]):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="Η συνεδρία ασφαλείας έληξε. Ανανεώστε τη σελίδα."), 400
                abort(400, description="Η συνεδρία ασφαλείας έληξε. Ανανεώστε τη σελίδα.")

    @app.after_request
    def secure_response(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.context_processor
    def template_helpers():
        def sort_url(column: str, current_sort: str, current_dir: str) -> str:
            params = request.args.to_dict(flat=True)
            params.update(request.view_args or {})
            params["sort"] = column
            params["dir"] = "desc" if current_sort == column and current_dir == "asc" else "asc"
            params.pop("page", None)
            params.pop("offset", None)
            params.pop("format", None)
            return url_for(request.endpoint, **params)

        def multi_sort_url(column: str, current_sorts: list[tuple[str, str]]) -> str:
            params = request.args.to_dict(flat=True)
            params.update(request.view_args or {})
            existing_direction = next(
                (direction for key, direction in current_sorts if key == column),
                None,
            )
            next_direction = "desc" if existing_direction == "asc" else "asc"
            updated_sorts = [(column, next_direction), *(
                (key, direction)
                for key, direction in current_sorts
                if key != column
            )]
            params["sort"] = ",".join(key for key, _ in updated_sorts)
            params["dir"] = ",".join(direction for _, direction in updated_sorts)
            params.pop("page", None)
            params.pop("offset", None)
            params.pop("format", None)
            return url_for(request.endpoint, **params)

        return {
            "csrf_token": session.get("_csrf_token", ""),
            "sort_url": sort_url,
            "multi_sort_url": multi_sort_url,
            "database_name": Path(app.config["DB_PATH"]).name,
            "database_path": app.config["DB_PATH"],
            "show_database_notice": _show_database_notice(app),
        }

    @app.route("/")
    def home():
        today_iso = date.today().isoformat()
        with db_conn(app) as con:
            stats = {
                "histories": con.execute(
                    "SELECT COUNT(*) FROM clinical_histories"
                ).fetchone()[0],
                "active_histories": con.execute(
                    "SELECT COUNT(*) FROM clinical_histories WHERE is_active=1"
                ).fetchone()[0],
                "current": con.execute(
                    """SELECT COUNT(*)
                       FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id
                       WHERE h.today=1 AND h.is_active=1 AND p.is_active=1"""
                ).fetchone()[0],
                "today_appointments": con.execute(
                    "SELECT COUNT(DISTINCT history_id) FROM appointments WHERE appointment_date=?",
                    (today_iso,),
                ).fetchone()[0],
            }
        return render_template("home.html", stats=stats)

    @app.route("/today")
    def today_appointments():
        raw_date = request.args.get("date", "").strip()
        try:
            selected_date = normalize_value(
                "appointments", "appointment_date", raw_date or date.today().isoformat(),
            )
        except ValidationError as exc:
            abort(400, description=str(exc))

        sort_map = {
            "patient_id": "p.patient_id",
            "history_id": "h.history_id",
            "last_name": "PYCASEFOLD(p.last_name)",
            "first_name": "PYCASEFOLD(p.first_name)",
            "coverage": "PYCASEFOLD(cp.name)",
            "gesy_referral": "PYCASEFOLD(gr.referral_number)",
            "birthdate": "p.birthdate",
            "identity_number": "PYCASEFOLD(p.identity_number)",
            "mobile": "PYCASEFOLD(p.mobile_phone)",
        }
        sort_key, sort_dir = validated_sort(sort_map, "last_name")
        offset = batch_offset()
        rows, has_more = batched_query(
            app,
            f"""SELECT a.appointment_id, p.patient_id, h.history_id,
                      p.last_name, p.first_name, p.birthdate,
                      p.identity_number, p.mobile_phone,
                      cp.name AS coverage_name, cp.coverage_type,
                      gr.referral_number, gr.allowed_visits,
                      (SELECT COUNT(*) FROM appointments used
                       WHERE used.gesy_referral_id=a.gesy_referral_id
                         AND used.status='completed') AS used_visits,
                      pay.payment_id, pay.amount_due, pay.copayment,
                      pay.amount_paid, pay.receipt_amount,
                      {effective_charge_sql()} AS effective_charge""",
            """FROM appointments a
               JOIN clinical_histories h ON h.history_id=a.history_id
               JOIN patients p ON p.patient_id=h.patient_id
               LEFT JOIN CoveragePlans cp ON cp.coverage_plan_id=a.coverage_plan_id
               LEFT JOIN GesyReferrals gr ON gr.gesy_referral_id=a.gesy_referral_id
               LEFT JOIN payments pay ON pay.appointment_id=a.appointment_id
               LEFT JOIN GesyMonth gm
                 ON gm.year=CAST(strftime('%Y',a.appointment_date) AS INTEGER)
                AND gm.month=CAST(strftime('%m',a.appointment_date) AS INTEGER)""",
            ["a.appointment_date=?"],
            [selected_date],
            sort_map[sort_key], sort_dir, offset, LIST_BATCH_SIZE,
            "a.appointment_id",
        )
        if wants_list_batch():
            return list_batch_response("_today_rows.html", rows, offset, has_more)
        visible_count = query_count(
            app,
            "FROM appointments a",
            ["a.appointment_date=?"],
            [selected_date],
        )
        total_count = query_count(app, "FROM appointments", [], [])
        return render_template(
            "today.html", rows=rows, selected_date=selected_date,
            has_more=has_more, next_offset=offset + len(rows),
            batch_size=LIST_BATCH_SIZE, current_sort=sort_key,
            current_dir=sort_dir, visible_count=visible_count, total_count=total_count,
        )

    @app.route("/active")
    def active_histories():
        q = request.args.get("q", "").strip()
        sort_map = {
            "patient_id": "p.patient_id", "history_id": "h.history_id",
            "first_name": "PYCASEFOLD(p.first_name)", "last_name": "PYCASEFOLD(p.last_name)",
            "history_date": "h.history_date", "diagnosis": "PYCASEFOLD(h.main_diagnosis)",
            "mobile": "p.mobile_phone", "patient_active": "p.is_active",
            "history_active": "h.is_active", "today": "h.today",
        }
        sort_key, sort_dir = validated_sort(sort_map, "last_name")
        offset = batch_offset()
        where: list[str] = []
        params: list[Any] = []
        add_normalized_search(
            where, params, q,
            ("CAST(h.history_id AS TEXT)", "CAST(p.patient_id AS TEXT)", "p.first_name", "p.last_name", "p.mobile_phone"),
            prefix_expressions=("p.first_name", "p.last_name"),
        )
        rows, has_more = batched_query(
            app,
            """SELECT h.history_id, h.patient_id, h.history_date, h.main_diagnosis, h.is_active,
                      h.today, h.social_security, h.gesy_referral,
                      p.first_name, p.last_name, p.birthdate, p.mobile_phone, p.home_phone,
                      p.is_active AS patient_active""",
            "FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id",
            where, params, sort_map[sort_key], sort_dir, offset, LIST_BATCH_SIZE,
            "p.patient_id, h.history_id",
        )
        if wants_list_batch():
            return list_batch_response(
                "_active_rows.html", rows, offset, has_more,
            )
        visible_count = query_count(
            app,
            "FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id",
            where, params,
        )
        total_count = query_count(app, "FROM clinical_histories", [], [])
        return render_template(
            "active.html", rows=rows, q=q, has_more=has_more,
            next_offset=offset + len(rows), batch_size=LIST_BATCH_SIZE,
            current_sort=sort_key, current_dir=sort_dir,
            visible_count=visible_count, total_count=total_count,
        )

    @app.route("/activation")
    def activation():
        q = request.args.get("q", "").strip()
        sort_map = {
            "patient_id": "p.patient_id", "history_id": "h.history_id",
            "first_name": "PYCASEFOLD(p.first_name)", "last_name": "PYCASEFOLD(p.last_name)",
            "history_date": "h.history_date", "diagnosis": "PYCASEFOLD(h.main_diagnosis)",
            "mobile": "p.mobile_phone", "history_active": "h.is_active",
            "today": "h.today",
        }
        current_sorts = validated_sorts(sort_map, "history_date", "desc")
        sort_state = {
            key: {"direction": direction, "priority": priority}
            for priority, (key, direction) in enumerate(current_sorts, start=1)
        }
        order_terms = [
            (sort_map[key], direction) for key, direction in current_sorts
        ]
        offset = batch_offset()
        where: list[str] = []
        params: list[Any] = []
        add_normalized_search(
            where, params, q,
            ("CAST(h.history_id AS TEXT)", "CAST(p.patient_id AS TEXT)", "p.first_name", "p.last_name", "p.mobile_phone"),
            prefix_expressions=("p.first_name", "p.last_name"),
        )
        rows, has_more = batched_query_multi(
            app,
            """SELECT h.history_id, h.patient_id, h.history_date, h.main_diagnosis, h.is_active,
                      h.today, p.first_name, p.last_name, p.mobile_phone""",
            "FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id",
            where, params, order_terms, offset, LIST_BATCH_SIZE,
            "h.history_id",
        )
        if wants_list_batch():
            return list_batch_response(
                "_activation_rows.html", rows, offset, has_more,
            )
        visible_count = query_count(
            app,
            "FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id",
            where, params,
        )
        total_count = query_count(app, "FROM clinical_histories", [], [])
        return render_template(
            "activation.html", rows=rows, q=q, has_more=has_more,
            next_offset=offset + len(rows), batch_size=LIST_BATCH_SIZE,
            current_sorts=current_sorts, sort_state=sort_state,
            visible_count=visible_count, total_count=total_count,
        )

    @app.route("/patients")
    def patients():
        q = request.args.get("q", "").strip()
        sort_map = {
            "patient_id": "p.patient_id", "first_name": "PYCASEFOLD(p.first_name)",
            "last_name": "PYCASEFOLD(p.last_name)", "mobile": "p.mobile_phone",
            "identity_number": "p.identity_number", "birthdate": "p.birthdate",
            "history_count": "history_count",
            "is_active": "p.is_active",
        }
        sort_key, sort_dir = validated_sort(sort_map, "last_name")
        offset = batch_offset()
        where: list[str] = []
        params: list[Any] = []
        add_normalized_search(
            where, params, q,
            ("CAST(p.patient_id AS TEXT)", "p.first_name", "p.last_name", "p.mobile_phone", "p.identity_number"),
            prefix_expressions=("p.first_name", "p.last_name"),
        )
        rows, has_more = batched_query(
            app,
            """SELECT p.patient_id, p.first_name, p.last_name, p.mobile_phone,
                      p.identity_number, p.birthdate, p.is_active,
                      COALESCE(hc.history_count, 0) AS history_count,
                      COALESCE(hc.active_history_count, 0) AS active_history_count""",
            """FROM patients p
               LEFT JOIN (
                   SELECT patient_id, COUNT(*) AS history_count,
                          SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active_history_count
                   FROM clinical_histories
                   GROUP BY patient_id
               ) hc ON hc.patient_id=p.patient_id""",
            where, params, sort_map[sort_key], sort_dir,
            offset, LIST_BATCH_SIZE, "p.patient_id",
        )
        if wants_list_batch():
            return list_batch_response(
                "_patient_rows.html", rows, offset, has_more,
            )
        visible_count = query_count(app, "FROM patients p", where, params)
        total_count = query_count(app, "FROM patients", [], [])
        return render_template(
            "patients.html", rows=rows, q=q, has_more=has_more,
            next_offset=offset + len(rows), batch_size=LIST_BATCH_SIZE,
            current_sort=sort_key, current_dir=sort_dir,
            visible_count=visible_count, total_count=total_count,
        )

    @app.route("/patients/new", methods=["GET", "POST"])
    def patient_new():
        defaults = {column: "" for column in PATIENT_FORM_COLUMNS}
        defaults.update({"is_active": "1", "referral_text": "", "profession_text": ""})
        values = {column: request.form.get(column, "") for column in PATIENT_FORM_COLUMNS}
        values.update({
            "referral_text": request.form.get("referral_text", ""),
            "profession_text": request.form.get("profession_text", ""),
        })
        if request.method == "GET":
            values.update(defaults)
            return render_template("patient_new.html", values=values)
        try:
            normalized = {
                column: normalize_value(
                    "patients", column,
                    request.form.get(column, 0 if column == "is_active" else None),
                )
                for column in PATIENT_FORM_COLUMNS
                if column not in {"referral_id", "profession_id"}
            }
            if not normalized["first_name"] and not normalized["last_name"]:
                raise ValidationError("Συμπληρώστε τουλάχιστον όνομα ή επώνυμο")
            columns = list(PATIENT_FORM_COLUMNS)
            placeholders = ",".join("?" for _ in columns)
            with write_transaction(app) as con:
                referral_id, new_referral = resolve_related_choice(
                    con, "referral", request.form.get("referral_id"),
                    values["referral_text"],
                )
                profession_id, new_profession = resolve_related_choice(
                    con, "profession", request.form.get("profession_id"),
                    values["profession_text"],
                )
                normalized["referral_id"] = normalize_value(
                    "patients", "referral_id", referral_id,
                )
                normalized["profession_id"] = normalize_value(
                    "patients", "profession_id", profession_id,
                )
                cur = con.execute(
                    f"INSERT INTO patients({','.join(columns)}) VALUES({placeholders})",
                    [normalized[column] for column in columns],
                )
                patient_id = cur.lastrowid
                created_related = [
                    item for item in (new_referral, new_profession) if item is not None
                ]
                log_change(
                    con, app, "insert", "patients", "patient_id", patient_id, None, None,
                    {"patient_id": patient_id, "created_related": created_related},
                    f"Δημιουργία ασθενούς #{patient_id}",
                )
            return redirect(url_for("patient_detail", patient_id=patient_id))
        except (ValidationError, sqlite3.IntegrityError) as exc:
            return render_template(
                "patient_new.html", values=values, error=str(exc),
                unsaved=values_differ_from_defaults(values, defaults),
            ), 400

    @app.route("/patients/<int:patient_id>")
    def patient_detail(patient_id: int):
        with db_conn(app) as con:
            patient = con.execute(
                """
                SELECT p.*,
                       TRIM(COALESCE(r.last_name,'') || ' ' || COALESCE(r.first_name,'')) AS referral_name,
                       pr.profession_name
                FROM patients p
                LEFT JOIN referrals r ON r.referral_id=p.referral_id
                LEFT JOIN professions pr ON pr.profession_id=p.profession_id
                WHERE p.patient_id=?
                """,
                (patient_id,),
            ).fetchone()
            if not patient:
                abort(404)
            histories = con.execute("""
                SELECT history_id, history_date, main_diagnosis, is_active, today
                FROM clinical_histories WHERE patient_id=?
                ORDER BY history_date DESC, history_id DESC
            """, (patient_id,)).fetchall()
        return render_template(
            "patient.html", patient=patient, histories=histories,
        )

    @app.route("/histories/new", methods=["GET", "POST"])
    def history_new_picker():
        patient_id = (
            request.form.get("patient_id", type=int)
            if request.method == "POST"
            else request.args.get("patient_id", type=int)
        )
        if not patient_id:
            return redirect(url_for("patients", choose_for_history=1))
        with db_conn(app) as con:
            patient = con.execute(
                "SELECT patient_id, first_name, last_name, mobile_phone, birthdate FROM patients WHERE patient_id=?",
                (patient_id,),
            ).fetchone()
            if not patient:
                abort(404)
        defaults = {column: "" for column in HISTORY_FORM_COLUMNS}
        defaults.update({
            "history_date": date.today().isoformat(), "is_active": "1", "today": "1",
            "doctor_text": "",
        })
        values = {column: request.form.get(column, "") for column in HISTORY_FORM_COLUMNS}
        values["doctor_text"] = request.form.get("doctor_text", "")
        if request.method == "GET":
            values.update(defaults)
            return render_template("history_new.html", patient=patient, values=values)
        try:
            normalized = {
                column: normalize_value(
                    "clinical_histories", column,
                    request.form.get(column, 0 if column in {"is_active", "today"} else None),
                )
                for column in HISTORY_FORM_COLUMNS
                if column != "doctor_id"
            }
            normalized["history_date"] = normalized["history_date"] or date.today().isoformat()
            if normalized["is_active"] != 1:
                normalized["today"] = 0
            columns = ["patient_id", *HISTORY_FORM_COLUMNS]
            with write_transaction(app) as con:
                doctor_id, new_doctor = resolve_related_choice(
                    con, "doctor", request.form.get("doctor_id"), values["doctor_text"],
                )
                normalized["doctor_id"] = normalize_value(
                    "clinical_histories", "doctor_id", doctor_id,
                )
                row_values = [patient_id, *[normalized[column] for column in HISTORY_FORM_COLUMNS]]
                cur = con.execute(
                    f"INSERT INTO clinical_histories({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    row_values,
                )
                history_id = cur.lastrowid
                con.execute(
                    "UPDATE patients SET is_active=1, updated_at=CURRENT_TIMESTAMP WHERE patient_id=?",
                    (patient_id,),
                )
                log_change(
                    con, app, "insert", "clinical_histories", "history_id", history_id,
                    None, None,
                    {"history_id": history_id, "patient_id": patient_id,
                     "created_related": [new_doctor] if new_doctor else []},
                    f"Δημιουργία ιστορικού #{history_id}",
                )
            return redirect(url_for("history_detail", history_id=history_id))
        except (ValidationError, sqlite3.IntegrityError) as exc:
            return render_template(
                "history_new.html", patient=patient, values=values, error=str(exc),
                unsaved=values_differ_from_defaults(values, defaults),
            ), 400

    @app.route("/histories/<int:history_id>")
    def history_detail(history_id: int):
        with db_conn(app) as con:
            history = con.execute("""
                SELECT h.*, p.first_name, p.last_name, p.mobile_phone, p.birthdate
                FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id
                WHERE h.history_id=?
            """, (history_id,)).fetchone()
            if not history:
                abort(404)
            doctors = con.execute(
                """SELECT doctor_id, first_name, last_name, specialty FROM doctors
                   ORDER BY PYCASEFOLD(last_name), PYCASEFOLD(first_name), doctor_id"""
            ).fetchall()
            gesy_referrals = referral_rows(con, history_id)
        return render_template(
            "history.html", history=history, doctors=doctors,
            gesy_referrals=gesy_referrals,
        )

    @app.route("/current")
    def current():
        history_id = request.args.get("history_id", type=int)
        current_view = request.args.get("view", "today")
        current_view_definitions = {
            "today": {
                "label": "Ημερήσια",
                "where": "h.today=1 AND h.is_active=1 AND p.is_active=1",
                "empty": "Δεν υπάρχουν ιστορικά με ενεργοποιημένη την επιλογή «Ημερήσια».",
            },
            "active_histories": {
                "label": "Ενεργά ιστορικά",
                "where": "h.is_active=1",
                "empty": "Δεν υπάρχουν ενεργά ιστορικά.",
            },
            "active_patients": {
                "label": "Ενεργός ασθενής",
                "where": "p.is_active=1",
                "empty": "Δεν υπάρχουν ιστορικά ενεργών ασθενών.",
            },
            "all": {
                "label": "Όλες οι εγγραφές",
                "where": "1=1",
                "empty": "Δεν υπάρχουν ιστορικά.",
            },
        }
        if current_view not in current_view_definitions:
            current_view = "today"
        current_view_definition = current_view_definitions[current_view]
        current_filters = [
            (key, current_view_definitions[key]["label"])
            for key in ("today", "active_histories")
        ]
        sort_map = {
            "number": "a.appointment_number", "date": "a.appointment_date",
            "due": "effective_charge", "paid": "pay.amount_paid",
            "receipt": "CAST(pay.receipt_amount AS REAL)", "notes": "PYCASEFOLD(a.notes)",
        }
        sort_key, sort_dir = validated_sort(sort_map, "number")
        with db_conn(app) as con:
            total_count = con.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
            current_rows = con.execute(f"""
                SELECT h.history_id, h.patient_id, h.history_date, h.main_diagnosis,
                       p.first_name, p.last_name
                FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id
                WHERE {current_view_definition["where"]}
                ORDER BY PYCASEFOLD(p.last_name), PYCASEFOLD(p.first_name),
                         h.history_date DESC, p.patient_id, h.history_id
            """).fetchall()
            history = None
            if history_id:
                history = con.execute("""
                    SELECT h.*, p.first_name, p.last_name, p.mobile_phone, p.birthdate,
                           p.identity_number, p.is_active AS patient_active
                    FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id
                    WHERE h.history_id=?
                """, (history_id,)).fetchone()
            if not history:
                if not current_rows:
                    return render_template(
                        "current.html", current_rows=[], history=None, appointments=[],
                        appointment_totals={"due": 0.0, "credit": 0.0, "receipts": 0.0},
                        current_view=current_view, current_view_label=current_view_definition["label"],
                        current_empty_message=current_view_definition["empty"], current_filters=current_filters,
                        current_sort=sort_key, current_dir=sort_dir,
                        visible_count=0, total_count=total_count,
                    )
                history_id = current_rows[0]["history_id"]
                history = con.execute("""
                    SELECT h.*, p.first_name, p.last_name, p.mobile_phone, p.birthdate,
                           p.identity_number, p.is_active AS patient_active
                    FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id
                    WHERE h.history_id=?
                """, (history_id,)).fetchone()
            appointments = con.execute(f"""
                SELECT a.appointment_id, a.appointment_number, a.appointment_date, a.appointment_time,
                       a.notes, a.status, a.today, a.coverage_plan_id, a.gesy_referral_id,
                       pay.payment_id, pay.amount_due, pay.copayment,
                       pay.amount_paid, pay.receipt_amount,
                       cp.code AS coverage_code, cp.name AS coverage_name,
                       cp.coverage_type, gr.referral_number, gr.allowed_visits,
                       (SELECT COUNT(*) FROM appointments used
                        WHERE used.gesy_referral_id=a.gesy_referral_id
                          AND used.status='completed') AS used_visits,
                       {effective_charge_sql()} AS effective_charge
                FROM appointments a
                LEFT JOIN payments pay ON pay.payment_id = (
                    SELECT p2.payment_id FROM payments p2
                    WHERE p2.appointment_id=a.appointment_id
                    ORDER BY p2.payment_id LIMIT 1
                )
                LEFT JOIN CoveragePlans cp ON cp.coverage_plan_id=a.coverage_plan_id
                LEFT JOIN GesyReferrals gr ON gr.gesy_referral_id=a.gesy_referral_id
                LEFT JOIN GesyMonth gm
                  ON gm.year=CAST(strftime('%Y',a.appointment_date) AS INTEGER)
                 AND gm.month=CAST(strftime('%m',a.appointment_date) AS INTEGER)
                WHERE a.history_id=?
                ORDER BY {sort_map[sort_key]} {sort_dir.upper()}, a.appointment_id
            """, (history_id,)).fetchall()
            plans = coverage_plans(con)
            gesy_referrals = referral_rows(con, history_id)

        def appointment_total(column: str) -> float:
            total = 0.0
            for appointment in appointments:
                raw_value = appointment[column]
                if raw_value in (None, ""):
                    continue
                try:
                    total += float(str(raw_value).strip().replace(",", "."))
                except ValueError:
                    continue
            return total

        appointment_totals = {
            "due": appointment_total("effective_charge"),
            "credit": appointment_total("amount_paid"),
            "receipts": appointment_total("receipt_amount"),
        }
        return render_template(
            "current.html", current_rows=current_rows, history=history, appointments=appointments,
            appointment_totals=appointment_totals,
            current_view=current_view, current_view_label=current_view_definition["label"],
            current_empty_message=current_view_definition["empty"], current_filters=current_filters,
            current_sort=sort_key, current_dir=sort_dir,
            visible_count=len(appointments), total_count=total_count,
            coverage_plans=plans, gesy_referrals=gesy_referrals,
        )

    @app.route("/settings")
    def settings():
        with db_conn(app) as con:
            plans = coverage_plans(con, active_only=False)
            gesy_months = con.execute("""
                SELECT gesy_month_id, year, month, rate
                FROM GesyMonth ORDER BY year DESC, month DESC
            """).fetchall()
        return render_template(
            "settings.html", health=data_health(app), backup=backup_status(app),
            database_path=app.config["DB_PATH"],
            first_gesy_amount=get_setting(app, "first_gesy_amount", "10.00"),
            first_other_amount=get_setting(app, "first_other_amount", "35.00"),
            coverage_plans=plans, gesy_months=gesy_months,
            appointment_settings={
                "calendar_start": get_setting(app, "appointment_calendar_start", "08:00"),
                "calendar_end": get_setting(app, "appointment_calendar_end", "20:00"),
                "morning_end": get_setting(app, "appointment_morning_end", "13:00"),
                "afternoon_start": get_setting(app, "appointment_afternoon_start", "15:00"),
                "step": get_setting(app, "appointment_step", "15"),
                "durations": get_setting(app, "appointment_durations", "30,45,60"),
                "default_duration": get_setting(app, "appointment_default_duration", "60"),
            },
        )

    def appointment_config() -> dict[str, Any]:
        raw_durations = get_setting(app, "appointment_durations", "30,45,60")
        try:
            durations = sorted({int(item) for item in raw_durations.split(",") if int(item) > 0})
        except ValueError:
            durations = [30, 45, 60]
        return {
            "calendar_start": get_setting(app, "appointment_calendar_start", "08:00"),
            "calendar_end": get_setting(app, "appointment_calendar_end", "20:00"),
            "morning_end": get_setting(app, "appointment_morning_end", "13:00"),
            "afternoon_start": get_setting(app, "appointment_afternoon_start", "15:00"),
            "step": int(get_setting(app, "appointment_step", "15")),
            "durations": durations or [60],
            "default_duration": int(get_setting(app, "appointment_default_duration", "60")),
        }

    def parse_calendar_date(raw: str | None) -> date:
        if not raw:
            return date.today()
        for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(str(raw).strip(), pattern).date()
            except ValueError:
                continue
        raise ValidationError("Η ημερομηνία ραντεβού δεν είναι έγκυρη")

    def valid_clock(raw: Any) -> str:
        try:
            return datetime.strptime(str(raw), "%H:%M").strftime("%H:%M")
        except ValueError as exc:
            raise ValidationError("Η ώρα πρέπει να είναι σε μορφή ΩΩ:ΛΛ") from exc

    def validate_appointment_slot(
        raw_date: Any, raw_time: Any, raw_duration: Any, config: dict[str, Any],
    ) -> tuple[str, str, int, datetime]:
        appointment_date = parse_calendar_date(raw_date).isoformat()
        start_time = valid_clock(raw_time)
        duration = int(raw_duration)
        if duration not in config["durations"]:
            raise ValidationError("Η διάρκεια δεν επιτρέπεται από τις ρυθμίσεις")
        start_dt = datetime.fromisoformat(f"{appointment_date}T{start_time}")
        end_dt = start_dt + timedelta(minutes=duration)
        start_minutes = start_dt.hour * 60 + start_dt.minute
        calendar_start = datetime.strptime(config["calendar_start"], "%H:%M")
        calendar_start_minutes = calendar_start.hour * 60 + calendar_start.minute
        if (start_minutes - calendar_start_minutes) % config["step"]:
            raise ValidationError(
                f"Η ώρα έναρξης πρέπει να ακολουθεί βήμα {config['step']} λεπτών"
            )
        if (
            start_time < config["calendar_start"]
            or end_dt.strftime("%H:%M") > config["calendar_end"]
            or end_dt.date().isoformat() != appointment_date
        ):
            raise ValidationError("Το ραντεβού πρέπει να είναι εντός του ωραρίου ημερολογίου")
        return appointment_date, start_time, duration, end_dt

    def same_day_warning(rows: list[sqlite3.Row], action: str) -> str:
        ranges = ", ".join(
            f"{row['start_time']}–"
            f"{(datetime.strptime(row['start_time'], '%H:%M') + timedelta(minutes=row['duration_minutes'])).strftime('%H:%M')}"
            for row in rows
        )
        return f"Ο ασθενής έχει ήδη ραντεβού την ίδια ημέρα στις {ranges}. {action}"

    @app.route("/future-appointments")
    def future_appointments():
        selected_date = parse_calendar_date(request.args.get("date"))
        view = request.args.get("view") or session.get("future_appointments_view", "week")
        if view not in {"week", "day"}: view = "week"
        session["future_appointments_view"] = view
        start = selected_date if view == "day" else selected_date - timedelta(days=selected_date.weekday())
        visible_days = 1 if view == "day" else 6
        end = start + timedelta(days=visible_days)
        selected_history = request.args.get("history_id", type=int)
        selected_patient = request.args.get("patient_id", type=int)
        search = (request.args.get("q") or "").strip()
        config = appointment_config()
        with db_conn(app) as con:
            where, params = ["h.is_active=1 AND p.is_active=1"], []
            if search:
                pattern = f"%{normalize_search_text(search)}%"
                where.append("(PYCASEFOLD(p.first_name) LIKE ? OR PYCASEFOLD(p.last_name) LIKE ? OR PYCASEFOLD(p.mobile_phone) LIKE ? OR PYCASEFOLD(h.main_diagnosis) LIKE ? OR PYCASEFOLD(h.problem_description) LIKE ?)")
                params.extend([pattern] * 5)
            active_histories = con.execute("""
                SELECT h.history_id,h.patient_id,h.main_diagnosis,p.first_name,p.last_name,p.mobile_phone
                FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id
                WHERE """ + " AND ".join(where) + " ORDER BY PYCASEFOLD(p.last_name),PYCASEFOLD(p.first_name),h.history_id", params).fetchall()
            recents = con.execute("""
                SELECT h.history_id,h.patient_id,h.main_diagnosis,p.first_name,p.last_name,MAX(a.appointment_date) AS last_visit
                FROM appointments a JOIN clinical_histories h ON h.history_id=a.history_id JOIN patients p ON p.patient_id=h.patient_id
                WHERE h.is_active=1 AND p.is_active=1 GROUP BY h.history_id ORDER BY last_visit DESC,a.appointment_id DESC LIMIT 8
            """).fetchall()
            scheduled = con.execute("""
                SELECT f.*,p.first_name,p.last_name,h.main_diagnosis FROM Future_appointments f
                JOIN patients p ON p.patient_id=f.patient_id JOIN clinical_histories h ON h.history_id=f.history_id
                WHERE f.appointment_date>=? AND f.appointment_date<? ORDER BY f.appointment_date,f.start_time,f.future_appointment_id
            """, (start.isoformat(), end.isoformat())).fetchall()
            plans = [dict(row) for row in coverage_plans(con)]
            referrals = [dict(row) for row in con.execute("""
                SELECT r.gesy_referral_id,r.history_id,r.referral_number,r.allowed_visits,
                       COUNT(CASE WHEN a.status='completed' THEN 1 END) AS used_visits
                FROM GesyReferrals r
                LEFT JOIN appointments a ON a.gesy_referral_id=r.gesy_referral_id
                GROUP BY r.gesy_referral_id ORDER BY r.gesy_referral_id DESC
            """).fetchall()]
        delta = 7 if view == "week" else 1
        return render_template("future_appointments.html", config=config, view=view, start=start, previous=start-timedelta(days=delta), next_date=start+timedelta(days=delta), days=[start + timedelta(days=i) for i in range(visible_days)], scheduled=[dict(row) for row in scheduled], active_histories=active_histories, recents=recents, selected_history=selected_history, selected_patient=selected_patient, search=search, coverage_plans=plans, gesy_referrals=referrals)

    @app.post("/api/database/create-empty")
    def api_create_empty_database():
        current_path = Path(app.config["DB_PATH"])
        try:
            destination = choose_database_file(
                current_path.parent, create_new=True,
            )
            if destination is None:
                return jsonify(ok=True, cancelled=True)
            created = create_empty_schema_database(current_path, destination)
            return jsonify(
                ok=True, cancelled=False, filename=created.name, path=str(created),
            )
        except ValidationError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            app.logger.exception("Αποτυχία δημιουργίας κενής βάσης")
            return jsonify(ok=False, error=str(exc)), 400

    @app.post("/api/database/select")
    def api_select_database():
        current_path = Path(app.config["DB_PATH"])
        try:
            selected = choose_database_file(current_path.parent)
            if selected is None:
                return jsonify(ok=True, cancelled=True)
            selected = validate_clinical_database(selected)
        except ValidationError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            app.logger.exception("Αποτυχία επιλογής βάσης εργασίας")
            return jsonify(ok=False, error=str(exc)), 400

        previous = {
            key: app.config[key]
            for key in ("DB_PATH", "META_DB_PATH", "DB_IDENTITY")
        }
        selected_meta = default_meta_path(selected)
        with app.config["DB_SWITCH_LOCK"]:
            try:
                init_meta_db(selected_meta)
                app.config.update(
                    DB_PATH=str(selected),
                    META_DB_PATH=str(selected_meta),
                    DB_IDENTITY=database_identity(selected),
                )
                migrate_receipt_amount(app)
                migrate_future_appointments(app)
                migrate_session_coverage(app)
                if app.config.get("AUTO_BACKUP"):
                    create_backup(app)
                save_database_selection(
                    app.config["DATABASE_SELECTION_PATH"], selected,
                )
            except Exception as exc:
                app.config.update(previous)
                app.logger.exception("Αποτυχία αλλαγής βάσης εργασίας")
                return jsonify(
                    ok=False,
                    error=f"Η βάση δεν άλλαξε. Η προηγούμενη επιλογή παραμένει ενεργή: {exc}",
                ), 500

        session.pop("startup_notice_token", None)
        return jsonify(
            ok=True, cancelled=False, filename=selected.name, path=str(selected),
        )

    @app.route("/statistics")
    def statistics():
        statistics_args: Any = request.args
        if not statistics_args:
            today = date.today()
            month_start = today.replace(day=1)
            next_month = (month_start + timedelta(days=32)).replace(day=1)
            statistics_args = {
                "from": month_start.isoformat(),
                "to": (next_month - timedelta(days=1)).isoformat(),
            }
        try:
            with db_conn(app) as con:
                stats = build_statistics(con, statistics_args)
        except ValueError as exc:
            return render_template("statistics.html", stats=None, error=str(exc)), 400
        return render_template("statistics.html", stats=stats, error=None)

    @app.get("/api/autocomplete")
    def api_autocomplete():
        field = request.args.get("field", "")
        source = AUTOCOMPLETE_SOURCES.get(field)
        query = request.args.get("q", "").strip()
        if not source:
            return jsonify(ok=True, suggestions=[])
        normalized_query = normalize_search_text(query[:200])
        pattern = "%" if not normalized_query else (
            f"{escape_like(normalized_query)}%"
            if source.get("prefix") else f"%{escape_like(normalized_query)}%"
        )
        value_sql = source["value"]
        prefix_parts = source.get("prefix_parts")
        match_sql = " OR ".join(
            f"PYCASEFOLD(TRIM(COALESCE({part},''))) LIKE ? ESCAPE '\\'"
            for part in prefix_parts
        ) if prefix_parts else f"PYCASEFOLD(TRIM({value_sql})) LIKE ? ESCAPE '\\'"
        match_params = [pattern] * len(prefix_parts) if prefix_parts else [pattern]
        with db_conn(app) as con:
            rows = con.execute(
                f"""
                SELECT MIN(TRIM({value_sql})) AS value,
                       MIN({source['id']}) AS item_id,
                       COUNT(*) AS frequency,
                       PYCASEFOLD(TRIM({value_sql})) AS normalized
                {source['from']}
                WHERE TRIM(COALESCE({value_sql},''))<>''
                  AND ({match_sql})
                GROUP BY PYCASEFOLD(TRIM({value_sql}))
                ORDER BY frequency DESC, normalized ASC
                LIMIT ?
                """,
                [*match_params, 15],
            ).fetchall()
        return jsonify(
            ok=True,
            suggestions=[
                {"value": row["value"], "id": row["item_id"], "frequency": row["frequency"]}
                for row in rows
            ],
        )

    @app.post("/api/settings")
    def api_settings():
        payload = json_payload()
        setting_fields = {
            "default_amount_due": "default_amount_due",
            "first_gesy_amount": "first_gesy_amount",
            "first_other_amount": "first_other_amount",
        }
        if "appointment_settings" in payload:
            supplied = payload["appointment_settings"]
            try:
                values = {
                    "appointment_calendar_start": valid_clock(supplied["calendar_start"]),
                    "appointment_calendar_end": valid_clock(supplied["calendar_end"]),
                    "appointment_morning_end": valid_clock(supplied["morning_end"]),
                    "appointment_afternoon_start": valid_clock(supplied["afternoon_start"]),
                }
                step = int(supplied["step"]); durations = sorted({int(value) for value in supplied["durations"]})
                default = int(supplied["default_duration"])
                if step != 15 or not durations or any(value not in {30, 45, 60} for value in durations) or default not in durations:
                    raise ValidationError("Ελέγξτε το βήμα, τις ενεργές διάρκειες και την προεπιλεγμένη διάρκεια")
                if not (values["appointment_calendar_start"] < values["appointment_morning_end"] <= values["appointment_afternoon_start"] < values["appointment_calendar_end"]):
                    raise ValidationError("Η έναρξη του ημερολογίου πρέπει να προηγείται του τέλους")
            except (KeyError, TypeError, ValueError) as exc:
                return jsonify(ok=False, error="Οι ρυθμίσεις ραντεβού δεν είναι έγκυρες"), 400
            values.update({"appointment_step": str(step), "appointment_durations": ",".join(map(str, durations)), "appointment_default_duration": str(default)})
            for key, value in values.items(): set_setting(app, key, value)
            return jsonify(ok=True, values=values)
        try:
            updates = {
                setting_key: validate_default_amount(payload[field])
                for field, setting_key in setting_fields.items()
                if field in payload
            }
        except ValidationError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        if not updates:
            return jsonify(ok=False, error="Δεν δόθηκε ρύθμιση"), 400
        for setting_key, value in updates.items():
            set_setting(app, setting_key, value)
        return jsonify(ok=True, value=updates.get("default_amount_due"), values=updates)

    @app.post("/api/coverage-plans")
    def api_create_coverage_plan():
        payload = json_payload()
        code = str(payload.get("code") or "").strip().upper()
        coverage_type = str(payload.get("coverage_type") or "").strip().upper()
        name = str(payload.get("name") or "").strip()
        if (
            not code or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in code)
            or coverage_type not in {"SELF_PAY", "PRIVATE_INSURANCE"}
            or not name
        ):
            return jsonify(ok=False, error="Ελέγξτε τον κωδικό, τον τύπο και το όνομα"), 400
        try:
            default_charge = float(validate_default_amount(payload.get("default_charge")))
        except ValidationError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        try:
            with write_transaction(app) as con:
                cur = con.execute("""
                    INSERT INTO CoveragePlans(
                        code, coverage_type, name, default_charge, active
                    ) VALUES(?,?,?,?,1)
                """, (code, coverage_type, name, default_charge))
        except sqlite3.IntegrityError:
            return jsonify(ok=False, error="Υπάρχει ήδη πλάνο με αυτόν τον κωδικό"), 409
        return jsonify(ok=True, coverage_plan_id=cur.lastrowid)

    @app.post("/api/coverage-plans/<int:coverage_plan_id>")
    def api_update_coverage_plan(coverage_plan_id: int):
        payload = json_payload()
        name = str(payload.get("name") or "").strip()
        active = 1 if payload.get("active") in (True, 1, "1", "true", "on") else 0
        if not name:
            return jsonify(ok=False, error="Το όνομα είναι υποχρεωτικό"), 400
        with write_transaction(app) as con:
            plan = con.execute(
                "SELECT coverage_type FROM CoveragePlans WHERE coverage_plan_id=?",
                (coverage_plan_id,),
            ).fetchone()
            if not plan:
                return jsonify(ok=False, error="Το πλάνο δεν βρέθηκε"), 404
            if plan["coverage_type"] == "GESY":
                default_charge = None
                active = 1
            else:
                try:
                    default_charge = float(validate_default_amount(payload.get("default_charge")))
                except ValidationError as exc:
                    return jsonify(ok=False, error=str(exc)), 400
            con.execute("""
                UPDATE CoveragePlans
                SET name=?, default_charge=?, active=?, updated_at=CURRENT_TIMESTAMP
                WHERE coverage_plan_id=?
            """, (name, default_charge, active, coverage_plan_id))
        return jsonify(ok=True)

    @app.post("/api/gesy-months")
    def api_upsert_gesy_month():
        payload = json_payload()
        try:
            year = int(payload.get("year"))
            month = int(payload.get("month"))
            rate = float(validate_default_amount(payload.get("rate")))
            if year < 1900 or year > 2200 or month < 1 or month > 12:
                raise ValidationError("Ο μήνας ή το έτος δεν είναι έγκυρο")
        except (TypeError, ValueError, ValidationError) as exc:
            return jsonify(ok=False, error=str(exc) or "Μη έγκυρη τιμή μήνα"), 400
        with write_transaction(app) as con:
            con.execute("""
                INSERT INTO GesyMonth(year,month,rate) VALUES(?,?,?)
                ON CONFLICT(year,month) DO UPDATE SET
                    rate=excluded.rate, updated_at=CURRENT_TIMESTAMP
            """, (year, month, rate))
        return jsonify(ok=True, year=year, month=month, rate=rate)

    @app.post("/api/histories/<int:history_id>/gesy-referrals")
    def api_create_gesy_referral(history_id: int):
        payload = json_payload()
        referral_number = str(payload.get("referral_number") or "").strip() or None
        notes = str(payload.get("notes") or "").strip() or None
        if not referral_number:
            return jsonify(ok=False, error="Ο αριθμός παραπεμπτικού είναι υποχρεωτικός"), 400
        try:
            allowed_visits = parse_positive_int(
                payload.get("allowed_visits"), "Επιτρεπόμενες επισκέψεις",
            )
        except ValidationError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        with write_transaction(app) as con:
            if not con.execute(
                "SELECT 1 FROM clinical_histories WHERE history_id=?", (history_id,),
            ).fetchone():
                return jsonify(ok=False, error="Το ιστορικό δεν βρέθηκε"), 404
            cur = con.execute("""
                INSERT INTO GesyReferrals(
                    history_id, referral_number, allowed_visits, notes
                ) VALUES(?,?,?,?)
            """, (history_id, referral_number, allowed_visits, notes))
        return jsonify(ok=True, gesy_referral_id=cur.lastrowid)

    @app.post("/api/future-appointments")
    def api_future_appointments():
        payload = json_payload()
        config = appointment_config()
        try:
            patient_id = int(payload.get("patient_id"))
            history_id = int(payload.get("history_id"))
            appointment_date, start_time, duration, end_dt = validate_appointment_slot(
                payload.get("appointment_date"), payload.get("start_time"),
                payload.get("duration_minutes"), config,
            )
            notes = str(payload.get("notes") or "").strip() or None
        except (TypeError, ValueError, ValidationError) as exc:
            return jsonify(ok=False, error=str(exc) or "Μη έγκυρα στοιχεία ραντεβού"), 400
        with write_transaction(app) as con:
            valid = con.execute(
                "SELECT 1 FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id "
                "WHERE h.history_id=? AND h.patient_id=?",
                (history_id, patient_id),
            ).fetchone()
            if not valid:
                return jsonify(ok=False, error="Το ιστορικό δεν ανήκει στον επιλεγμένο ασθενή"), 400
            try:
                plan, referral, unused = validate_coverage(
                    con, history_id, payload.get("coverage_plan_id"),
                    payload.get("gesy_referral_id"), completing=False,
                )
            except ValidationError as exc:
                return jsonify(ok=False, error=str(exc)), 400
            overlap = con.execute("""
                SELECT 1 FROM Future_appointments
                WHERE appointment_date=? AND status IN ('scheduled','completed')
                  AND time(start_time) < time(?)
                  AND time(start_time, '+' || duration_minutes || ' minutes') > time(?)
                LIMIT 1
            """, (appointment_date, end_dt.strftime("%H:%M"), start_time)).fetchone()
            if overlap:
                return jsonify(
                    ok=False,
                    error="Δεν υπάρχει διαθέσιμο συνεχόμενο διάστημα σε αυτή την ώρα",
                ), 409
            existing = con.execute("""
                SELECT start_time,duration_minutes FROM Future_appointments
                WHERE patient_id=? AND appointment_date=? AND status='scheduled'
                ORDER BY start_time
            """, (patient_id, appointment_date)).fetchall()
            if existing and not payload.get("confirm_second"):
                return jsonify(
                    ok=False,
                    requires_confirmation=True,
                    message=same_day_warning(
                        existing, "Θέλετε να καταχωρηθεί δεύτερο ραντεβού;",
                    ),
                ), 409
            cur = con.execute("""
                INSERT INTO Future_appointments(
                    patient_id,history_id,appointment_date,start_time,
                    duration_minutes,status,notes,coverage_plan_id,gesy_referral_id
                ) VALUES(?,?,?,?,?,'scheduled',?,?,?)
            """, (
                patient_id, history_id, appointment_date, start_time, duration, notes,
                plan["coverage_plan_id"],
                referral["gesy_referral_id"] if referral else None,
            ))
        return jsonify(ok=True, future_appointment_id=cur.lastrowid)

    @app.post("/api/future-appointments/<int:future_appointment_id>/cancel")
    def api_cancel_future_appointment(future_appointment_id: int):
        with write_transaction(app) as con:
            exists = con.execute(
                "SELECT 1 FROM Future_appointments WHERE future_appointment_id=?",
                (future_appointment_id,),
            ).fetchone()
            if not exists:
                return jsonify(ok=False, error="Το ραντεβού δεν βρέθηκε"), 404
            changed = con.execute(
                "UPDATE Future_appointments SET status='cancelled',"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE future_appointment_id=? AND status!='cancelled'",
                (future_appointment_id,),
            ).rowcount
        return jsonify(ok=True, changed=changed)

    @app.post("/api/future-appointments/<int:future_appointment_id>")
    def api_update_future_appointment(future_appointment_id: int):
        payload = json_payload()
        config = appointment_config()
        try:
            appointment_date, start_time, duration, end_dt = validate_appointment_slot(
                payload.get("appointment_date"), payload.get("start_time"),
                payload.get("duration_minutes"), config,
            )
            status = str(payload.get("status") or "scheduled")
            if status not in {"scheduled", "completed", "cancelled", "no_show"}:
                raise ValidationError("Η κατάσταση δεν είναι έγκυρη")
        except (TypeError, ValueError, ValidationError) as exc:
            return jsonify(ok=False, error=str(exc) or "Μη έγκυρα στοιχεία ραντεβού"), 400
        with write_transaction(app) as con:
            current = con.execute("SELECT * FROM Future_appointments WHERE future_appointment_id=?", (future_appointment_id,)).fetchone()
            if not current:
                return jsonify(ok=False, error="Το ραντεβού δεν βρέθηκε"), 404
            overlap = con.execute("""
                SELECT 1 FROM Future_appointments
                WHERE future_appointment_id<>? AND appointment_date=?
                  AND status IN ('scheduled','completed')
                  AND time(start_time) < time(?)
                  AND time(start_time, '+' || duration_minutes || ' minutes') > time(?)
                LIMIT 1
            """, (future_appointment_id, appointment_date, end_dt.strftime("%H:%M"), start_time)).fetchone()
            if status in {"scheduled", "completed"} and overlap:
                return jsonify(ok=False, error="Δεν υπάρχει διαθέσιμο συνεχόμενο διάστημα σε αυτή την ώρα"), 409
            existing = con.execute("""
                SELECT start_time,duration_minutes FROM Future_appointments
                WHERE future_appointment_id<>? AND patient_id=?
                  AND appointment_date=? AND status='scheduled'
                ORDER BY start_time
            """, (future_appointment_id, current["patient_id"], appointment_date)).fetchall()
            if status == "scheduled" and existing and not payload.get("confirm_second"):
                return jsonify(
                    ok=False,
                    requires_confirmation=True,
                    message=same_day_warning(
                        existing, "Θέλετε να αποθηκευτεί η αλλαγή;",
                    ),
                ), 409
            plan_id = payload.get("coverage_plan_id", current["coverage_plan_id"])
            referral_id = payload.get("gesy_referral_id", current["gesy_referral_id"])
            try:
                plan, referral, used_visits = validate_coverage(
                    con, current["history_id"], plan_id, referral_id,
                    completing=(status == "completed" and not current["completed_appointment_id"]),
                )
            except ValidationError as exc:
                return jsonify(ok=False, error=str(exc)), 400

            completed_appointment_id = current["completed_appointment_id"]
            exhausted = False
            if status == "completed" and not completed_appointment_id:
                try:
                    amount_due, copayment, effective_charge = financial_values_for_new_session(
                        con, plan, appointment_date, payload.get("copayment"),
                    )
                except ValidationError as exc:
                    return jsonify(ok=False, error=str(exc)), 400
                next_no = con.execute(
                    "SELECT COALESCE(MAX(appointment_number),0)+1 FROM appointments WHERE history_id=?",
                    (current["history_id"],),
                ).fetchone()[0]
                completed = con.execute("""
                    INSERT INTO appointments(
                        history_id,appointment_number,appointment_date,appointment_time,
                        status,today,coverage_plan_id,gesy_referral_id,notes
                    ) VALUES(?,?,?,?, 'completed',0,?,?,?)
                """, (
                    current["history_id"], next_no, appointment_date, start_time,
                    plan["coverage_plan_id"],
                    referral["gesy_referral_id"] if referral else None,
                    str(payload.get("notes") or "").strip() or None,
                ))
                completed_appointment_id = completed.lastrowid
                con.execute("""
                    INSERT INTO payments(
                        appointment_id,payment_date,amount_due,copayment,
                        amount_paid,receipt_amount
                    ) VALUES(?,?,?,?,0,0)
                """, (completed_appointment_id, appointment_date, amount_due, copayment))
                exhausted = bool(
                    referral and referral["allowed_visits"] is not None
                    and used_visits + 1 == referral["allowed_visits"]
                )
            con.execute("""
                UPDATE Future_appointments SET
                    appointment_date=?,start_time=?,duration_minutes=?,status=?,notes=?,
                    coverage_plan_id=?,gesy_referral_id=?,completed_appointment_id=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE future_appointment_id=?
            """, (
                appointment_date, start_time, duration, status,
                str(payload.get("notes") or "").strip() or None,
                plan["coverage_plan_id"],
                referral["gesy_referral_id"] if referral else None,
                completed_appointment_id, future_appointment_id,
            ))
        return jsonify(
            ok=True, completed_appointment_id=completed_appointment_id,
            referral_exhausted=exhausted,
        )

    @app.post("/api/patients/<int:patient_id>/delete")
    def api_delete_patient(patient_id: int):
        with db_conn(app) as con:
            if not con.execute(
                "SELECT 1 FROM patients WHERE patient_id=?", (patient_id,)
            ).fetchone():
                return jsonify(ok=False, error="Ο ασθενής δεν βρέθηκε"), 404

        try:
            backup_path = create_backup(app, force=True)
        except Exception:
            app.logger.exception("Αποτυχία backup πριν από διαγραφή ασθενή")
            return jsonify(
                ok=False,
                error="Η διαγραφή ακυρώθηκε επειδή δεν δημιουργήθηκε backup",
            ), 500

        try:
            with write_transaction(app) as con:
                patient = con.execute(
                    "SELECT * FROM patients WHERE patient_id=?", (patient_id,)
                ).fetchone()
                if not patient:
                    return jsonify(ok=False, error="Ο ασθενής δεν βρέθηκε"), 404

                histories = con.execute(
                    "SELECT * FROM clinical_histories WHERE patient_id=? ORDER BY history_id",
                    (patient_id,),
                ).fetchall()
                appointments = con.execute("""
                    SELECT a.* FROM appointments a
                    JOIN clinical_histories h ON h.history_id=a.history_id
                    WHERE h.patient_id=? ORDER BY a.appointment_id
                """, (patient_id,)).fetchall()
                payments = con.execute("""
                    SELECT pay.* FROM payments pay
                    JOIN appointments a ON a.appointment_id=pay.appointment_id
                    JOIN clinical_histories h ON h.history_id=a.history_id
                    WHERE h.patient_id=? ORDER BY pay.payment_id
                """, (patient_id,)).fetchall()
                future_appointments = con.execute("""
                    SELECT * FROM Future_appointments
                    WHERE patient_id=? ORDER BY future_appointment_id
                """, (patient_id,)).fetchall()
                gesy_referrals = con.execute("""
                    SELECT r.* FROM GesyReferrals r
                    JOIN clinical_histories h ON h.history_id=r.history_id
                    WHERE h.patient_id=? ORDER BY r.gesy_referral_id
                """, (patient_id,)).fetchall()

                snapshot = {
                    "patients": [dict(patient)],
                    "clinical_histories": [dict(row) for row in histories],
                    "GesyReferrals": [dict(row) for row in gesy_referrals],
                    "appointments": [dict(row) for row in appointments],
                    "Future_appointments": [dict(row) for row in future_appointments],
                    "payments": [dict(row) for row in payments],
                }
                log_change(
                    con, app, "delete_patient", "patients", "patient_id", patient_id,
                    None, None, snapshot, f"Διαγραφή ασθενή #{patient_id}",
                )

                deleted_payments = con.execute("""
                    DELETE FROM payments WHERE appointment_id IN (
                        SELECT a.appointment_id FROM appointments a
                        JOIN clinical_histories h ON h.history_id=a.history_id
                        WHERE h.patient_id=?
                    )
                """, (patient_id,)).rowcount
                deleted_future_appointments = con.execute(
                    "DELETE FROM Future_appointments WHERE patient_id=?", (patient_id,)
                ).rowcount
                deleted_appointments = con.execute("""
                    DELETE FROM appointments WHERE history_id IN (
                        SELECT history_id FROM clinical_histories WHERE patient_id=?
                    )
                """, (patient_id,)).rowcount
                deleted_gesy_referrals = con.execute("""
                    DELETE FROM GesyReferrals WHERE history_id IN (
                        SELECT history_id FROM clinical_histories WHERE patient_id=?
                    )
                """, (patient_id,)).rowcount
                deleted_histories = con.execute(
                    "DELETE FROM clinical_histories WHERE patient_id=?", (patient_id,)
                ).rowcount
                deleted_patient = con.execute(
                    "DELETE FROM patients WHERE patient_id=?", (patient_id,)
                ).rowcount
                if deleted_patient != 1:
                    raise sqlite3.IntegrityError("Ο ασθενής δεν διαγράφηκε")

            return jsonify(
                ok=True,
                patient_id=patient_id,
                histories=deleted_histories,
                appointments=deleted_appointments,
                future_appointments=deleted_future_appointments,
                gesy_referrals=deleted_gesy_referrals,
                payments=deleted_payments,
                backup=backup_path.name,
            )
        except sqlite3.Error:
            app.logger.exception("Αποτυχία διαγραφής ασθενή #%s", patient_id)
            return jsonify(
                ok=False,
                error="Η διαγραφή ακυρώθηκε και δεν άλλαξε κανένα δεδομένο",
            ), 500

    @app.post("/api/histories/<int:history_id>/delete")
    def api_delete_history(history_id: int):
        with db_conn(app) as con:
            if not con.execute(
                "SELECT 1 FROM clinical_histories WHERE history_id=?", (history_id,)
            ).fetchone():
                return jsonify(ok=False, error="Το ιστορικό δεν βρέθηκε"), 404

        try:
            backup_path = create_backup(app, force=True)
        except Exception:
            app.logger.exception("Αποτυχία backup πριν από διαγραφή ιστορικού")
            return jsonify(
                ok=False,
                error="Η διαγραφή ακυρώθηκε επειδή δεν δημιουργήθηκε backup",
            ), 500

        try:
            with write_transaction(app) as con:
                history = con.execute(
                    "SELECT * FROM clinical_histories WHERE history_id=?", (history_id,)
                ).fetchone()
                if not history:
                    return jsonify(ok=False, error="Το ιστορικό δεν βρέθηκε"), 404
                appointments = con.execute(
                    "SELECT * FROM appointments WHERE history_id=? ORDER BY appointment_id", (history_id,)
                ).fetchall()
                payments = con.execute("""
                    SELECT pay.* FROM payments pay
                    JOIN appointments a ON a.appointment_id=pay.appointment_id
                    WHERE a.history_id=? ORDER BY pay.payment_id
                """, (history_id,)).fetchall()
                future_appointments = con.execute(
                    "SELECT * FROM Future_appointments WHERE history_id=? "
                    "ORDER BY future_appointment_id", (history_id,),
                ).fetchall()
                gesy_referrals = con.execute(
                    "SELECT * FROM GesyReferrals WHERE history_id=? "
                    "ORDER BY gesy_referral_id", (history_id,),
                ).fetchall()
                snapshot = {
                    "clinical_histories": [dict(history)],
                    "GesyReferrals": [dict(row) for row in gesy_referrals],
                    "appointments": [dict(row) for row in appointments],
                    "Future_appointments": [dict(row) for row in future_appointments],
                    "payments": [dict(row) for row in payments],
                }
                log_change(
                    con, app, "delete_history", "clinical_histories", "history_id", history_id,
                    None, None, snapshot, f"Διαγραφή ιστορικού #{history_id}",
                )
                deleted_payments = con.execute("""
                    DELETE FROM payments WHERE appointment_id IN (
                        SELECT appointment_id FROM appointments WHERE history_id=?
                    )
                """, (history_id,)).rowcount
                deleted_future_appointments = con.execute(
                    "DELETE FROM Future_appointments WHERE history_id=?", (history_id,)
                ).rowcount
                deleted_appointments = con.execute(
                    "DELETE FROM appointments WHERE history_id=?", (history_id,)
                ).rowcount
                deleted_gesy_referrals = con.execute(
                    "DELETE FROM GesyReferrals WHERE history_id=?", (history_id,)
                ).rowcount
                deleted_history = con.execute(
                    "DELETE FROM clinical_histories WHERE history_id=?", (history_id,)
                ).rowcount
                if deleted_history != 1:
                    raise sqlite3.IntegrityError("Το ιστορικό δεν διαγράφηκε")
            return jsonify(
                ok=True, history_id=history_id, patient_id=history["patient_id"],
                appointments=deleted_appointments,
                future_appointments=deleted_future_appointments,
                gesy_referrals=deleted_gesy_referrals,
                payments=deleted_payments,
                backup=backup_path.name,
            )
        except sqlite3.Error:
            app.logger.exception("Αποτυχία διαγραφής ιστορικού #%s", history_id)
            return jsonify(
                ok=False,
                error="Η διαγραφή ακυρώθηκε και δεν άλλαξε κανένα δεδομένο",
            ), 500

    @app.post("/api/backup")
    def api_backup():
        try:
            path = create_backup(app, force=True)
            return jsonify(ok=True, filename=path.name, created_at=datetime.now().strftime("%d/%m/%Y %H:%M"))
        except Exception:
            app.logger.exception("Αποτυχία χειροκίνητου backup")
            return jsonify(ok=False, error="Δεν ήταν δυνατή η δημιουργία backup"), 500

    @app.post("/api/appointments/<int:appointment_id>/delete")
    def api_delete_appointment(appointment_id: int):
        with db_conn(app) as con:
            if not con.execute(
                "SELECT 1 FROM appointments WHERE appointment_id=?", (appointment_id,)
            ).fetchone():
                return jsonify(ok=False, error="Η παρουσία δεν βρέθηκε"), 404

        try:
            backup_path = create_backup(app, force=True)
        except Exception:
            app.logger.exception("Αποτυχία backup πριν από διαγραφή παρουσίας")
            return jsonify(
                ok=False,
                error="Η διαγραφή ακυρώθηκε επειδή δεν δημιουργήθηκε backup",
            ), 500

        try:
            with write_transaction(app) as con:
                appointment = con.execute(
                    "SELECT * FROM appointments WHERE appointment_id=?", (appointment_id,)
                ).fetchone()
                if not appointment:
                    return jsonify(ok=False, error="Η παρουσία δεν βρέθηκε"), 404
                payments = con.execute(
                    "SELECT * FROM payments WHERE appointment_id=? ORDER BY payment_id",
                    (appointment_id,),
                ).fetchall()
                future_appointment_links = [
                    row[0] for row in con.execute(
                        "SELECT future_appointment_id FROM Future_appointments "
                        "WHERE completed_appointment_id=? ORDER BY future_appointment_id",
                        (appointment_id,),
                    ).fetchall()
                ]
                snapshot = {
                    "appointments": [dict(appointment)],
                    "payments": [dict(row) for row in payments],
                    "future_appointment_links": future_appointment_links,
                }
                log_change(
                    con, app, "delete_appointment", "appointments", "appointment_id",
                    appointment_id, None, None, snapshot,
                    f"Διαγραφή παρουσίας #{appointment['appointment_number'] or appointment_id} "
                    f"από το ιστορικό #{appointment['history_id']}",
                )
                deleted_payments = con.execute(
                    "DELETE FROM payments WHERE appointment_id=?", (appointment_id,)
                ).rowcount
                con.execute(
                    "UPDATE Future_appointments SET completed_appointment_id=NULL,"
                    "updated_at=CURRENT_TIMESTAMP WHERE completed_appointment_id=?",
                    (appointment_id,),
                )
                deleted_appointment = con.execute(
                    "DELETE FROM appointments WHERE appointment_id=?", (appointment_id,)
                ).rowcount
                if deleted_appointment != 1:
                    raise sqlite3.IntegrityError("Η παρουσία δεν διαγράφηκε")
            return jsonify(
                ok=True, appointment_id=appointment_id,
                payments=deleted_payments, backup=backup_path.name,
            )
        except sqlite3.Error:
            app.logger.exception("Αποτυχία διαγραφής παρουσίας #%s", appointment_id)
            return jsonify(
                ok=False,
                error="Η διαγραφή ακυρώθηκε και δεν άλλαξε κανένα δεδομένο",
            ), 500

    @app.post("/api/update")
    def api_update():
        payload = json_payload()
        table, pk, column = payload.get("table"), payload.get("pk"), payload.get("column")
        if table not in ALLOWED_COLUMNS or column not in ALLOWED_COLUMNS[table]:
            return jsonify(ok=False, error="Μη επιτρεπτό πεδίο"), 400
        try:
            pk = int(pk)
            value = normalize_value(table, column, payload.get("value"))
        except (TypeError, ValueError, ValidationError) as exc:
            return jsonify(ok=False, error=str(exc) or "Μη έγκυρη τιμή"), 400
        pk_name = PK_COLUMNS[table]
        try:
            with write_transaction(app) as con:
                old_row = con.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE "{pk_name}"=?', (pk,)
                ).fetchone()
                if not old_row:
                    return jsonify(ok=False, error="Η εγγραφή δεν βρέθηκε"), 404
                old = old_row[0]

                if (
                    table == "appointments" and column == "appointment_date"
                    and value is not None and not equivalent(old, value)
                ):
                    duplicate = con.execute("""
                        SELECT 1
                        FROM appointments current_appointment
                        JOIN appointments other
                          ON other.history_id=current_appointment.history_id
                         AND other.appointment_id<>current_appointment.appointment_id
                        WHERE current_appointment.appointment_id=?
                          AND other.appointment_date=?
                        LIMIT 1
                    """, (pk, value)).fetchone()
                    if duplicate:
                        return jsonify(
                            ok=False,
                            error="Υπάρχει ήδη καταχώριση παρουσίας για αυτό το ιστορικό την επιλεγμένη ημερομηνία.",
                        ), 409
                    coverage = con.execute("""
                        SELECT cp.coverage_type FROM appointments a
                        LEFT JOIN CoveragePlans cp ON cp.coverage_plan_id=a.coverage_plan_id
                        WHERE a.appointment_id=?
                    """, (pk,)).fetchone()
                    if coverage and coverage["coverage_type"] == "GESY":
                        try:
                            ensure_gesy_month(con, value)
                        except ValidationError as exc:
                            return jsonify(ok=False, error=str(exc)), 400

                if table == "payments" and column in {"amount_due", "copayment"}:
                    coverage = con.execute("""
                        SELECT cp.coverage_type, a.appointment_date FROM payments pay
                        JOIN appointments a ON a.appointment_id=pay.appointment_id
                        LEFT JOIN CoveragePlans cp ON cp.coverage_plan_id=a.coverage_plan_id
                        WHERE pay.payment_id=?
                    """, (pk,)).fetchone()
                    if coverage and coverage["coverage_type"] == "GESY" and column == "amount_due":
                        return jsonify(ok=False, error="Η χρέωση ΓεΣΥ υπολογίζεται από το μηνιαίο Rate"), 400
                    if coverage and coverage["coverage_type"] != "GESY" and column == "copayment":
                        return jsonify(ok=False, error="Η συμπληρωμή ισχύει μόνο για ΓεΣΥ"), 400
                    if coverage and coverage["coverage_type"] == "GESY" and column == "copayment":
                        rate = ensure_gesy_month(con, coverage["appointment_date"])
                        if value is not None and value > rate:
                            return jsonify(ok=False, error="Η συμπληρωμή δεν μπορεί να υπερβαίνει τη χρέωση ΓεΣΥ"), 400

                if table == "clinical_histories" and column == "today" and value == 1:
                    eligibility = con.execute("""
                        SELECT h.is_active AS history_active, p.is_active AS patient_active
                        FROM clinical_histories h JOIN patients p ON p.patient_id=h.patient_id
                        WHERE h.history_id=?
                    """, (pk,)).fetchone()
                    if not eligibility or eligibility["history_active"] != 1 or eligibility["patient_active"] != 1:
                        return jsonify(
                            ok=False,
                            error="Η Ημερήσια επιλογή επιτρέπεται μόνο σε ενεργό ιστορικό ενεργού ασθενή",
                        ), 400

                cleared_today_history_ids: list[int] = []
                if column == "is_active" and value == 0:
                    if table == "patients":
                        cleared_today_history_ids = [
                            row["history_id"] for row in con.execute(
                                "SELECT history_id FROM clinical_histories WHERE patient_id=? AND today=1",
                                (pk,),
                            ).fetchall()
                        ]
                    elif table == "clinical_histories":
                        today_row = con.execute(
                            "SELECT today FROM clinical_histories WHERE history_id=?", (pk,)
                        ).fetchone()
                        if today_row and today_row["today"] == 1:
                            cleared_today_history_ids = [pk]

                if equivalent(old, value) and not cleared_today_history_ids:
                    return jsonify(ok=True, unchanged=True, value=value)
                if not equivalent(old, value):
                    con.execute(
                        f'UPDATE "{table}" SET "{column}"=?, updated_at=CURRENT_TIMESTAMP WHERE "{pk_name}"=?',
                        (value, pk),
                    )
                if cleared_today_history_ids:
                    placeholders = ",".join("?" for _ in cleared_today_history_ids)
                    con.execute(
                        f"UPDATE clinical_histories SET today=0, updated_at=CURRENT_TIMESTAMP "
                        f"WHERE history_id IN ({placeholders})",
                        cleared_today_history_ids,
                    )
                    log_change(
                        con, app, "update_status_cascade", table, pk_name, pk, column, old,
                        {"value": value, "cleared_today_history_ids": cleared_today_history_ids},
                        describe_update(table, column, pk),
                    )
                else:
                    log_change(
                        con, app, "update", table, pk_name, pk, column, old, value,
                        describe_update(table, column, pk),
                    )
            return jsonify(ok=True, value=value)
        except sqlite3.IntegrityError:
            return jsonify(ok=False, error="Η τιμή παραβιάζει σχέση της βάσης δεδομένων"), 400

    @app.post("/api/related-choice")
    def api_related_choice():
        payload = json_payload()
        table = payload.get("table")
        column = payload.get("column")
        kind = payload.get("kind")
        if (table, column, kind) != ("patients", "profession_id", "profession"):
            return jsonify(ok=False, error="Μη επιτρεπτό συσχετισμένο πεδίο"), 400
        try:
            pk = int(payload.get("pk"))
            if pk <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Μη έγκυρη εγγραφή"), 400

        try:
            with write_transaction(app) as con:
                old_row = con.execute(
                    "SELECT profession_id FROM patients WHERE patient_id=?", (pk,)
                ).fetchone()
                if not old_row:
                    return jsonify(ok=False, error="Η εγγραφή δεν βρέθηκε"), 404
                old = old_row[0]
                profession_id, new_profession = resolve_related_choice(
                    con, "profession", payload.get("value"), payload.get("text"),
                )
                value = normalize_value("patients", "profession_id", profession_id)
                display = ""
                if value is not None:
                    display_row = con.execute(
                        "SELECT profession_name FROM professions WHERE profession_id=?",
                        (value,),
                    ).fetchone()
                    if not display_row:
                        raise ValidationError("Το επάγγελμα δεν βρέθηκε")
                    display = display_row[0]
                if equivalent(old, value):
                    return jsonify(ok=True, unchanged=True, value=value, display=display)
                con.execute(
                    "UPDATE patients SET profession_id=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE patient_id=?",
                    (value, pk),
                )
                log_change(
                    con, app, "update_related", "patients", "patient_id", pk,
                    "profession_id", old,
                    {"value": value, "created_related": [new_profession] if new_profession else []},
                    describe_update("patients", "profession_id", pk),
                )
            return jsonify(ok=True, value=value, display=display)
        except (ValidationError, sqlite3.IntegrityError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @app.post("/api/activate/<int:history_id>")
    def api_activate(history_id: int):
        with write_transaction(app) as con:
            row = con.execute(
                "SELECT patient_id, is_active FROM clinical_histories WHERE history_id=?", (history_id,)
            ).fetchone()
            if not row:
                return jsonify(ok=False, error="Το ιστορικό δεν βρέθηκε"), 404
            patient_id, old_hist = row["patient_id"], row["is_active"]
            old_patient = con.execute(
                "SELECT is_active FROM patients WHERE patient_id=?", (patient_id,)
            ).fetchone()[0]
            if old_hist != 1 or old_patient != 1:
                con.execute(
                    "UPDATE clinical_histories SET is_active=1, updated_at=CURRENT_TIMESTAMP WHERE history_id=?",
                    (history_id,),
                )
                con.execute(
                    "UPDATE patients SET is_active=1, updated_at=CURRENT_TIMESTAMP WHERE patient_id=?",
                    (patient_id,),
                )
                log_change(
                    con, app, "activate_history", "clinical_histories", "history_id", history_id,
                    None, None,
                    {"history_id": history_id, "old_history_active": old_hist,
                     "patient_id": patient_id, "old_patient_active": old_patient},
                    f"Ενεργοποίηση ιστορικού #{history_id}",
                )
        return jsonify(ok=True)

    @app.post("/api/deactivate/<int:history_id>")
    def api_deactivate(history_id: int):
        with write_transaction(app) as con:
            row = con.execute(
                "SELECT is_active, today FROM clinical_histories WHERE history_id=?", (history_id,)
            ).fetchone()
            if not row:
                return jsonify(ok=False, error="Το ιστορικό δεν βρέθηκε"), 404
            old = row["is_active"]
            cleared_today_history_ids = [history_id] if row["today"] == 1 else []
            if old != 0 or cleared_today_history_ids:
                con.execute(
                    "UPDATE clinical_histories SET is_active=0, today=0, updated_at=CURRENT_TIMESTAMP WHERE history_id=?",
                    (history_id,),
                )
                if cleared_today_history_ids:
                    log_change(
                        con, app, "update_status_cascade", "clinical_histories", "history_id", history_id,
                        "is_active", old,
                        {"value": 0, "cleared_today_history_ids": cleared_today_history_ids},
                        f"Απενεργοποίηση ιστορικού #{history_id}",
                    )
                else:
                    log_change(
                        con, app, "update", "clinical_histories", "history_id", history_id,
                        "is_active", old, 0, f"Απενεργοποίηση ιστορικού #{history_id}",
                    )
        return jsonify(ok=True)

    @app.post("/api/appointment/new/<int:history_id>")
    def api_new_appointment(history_id: int):
        payload = json_payload()
        with write_transaction(app) as con:
            raw_date = payload.get("appointment_date") or date.today().isoformat()
            try:
                appointment_date = parse_calendar_date(raw_date).isoformat()
            except ValidationError as exc:
                return jsonify(ok=False, error=str(exc)), 400
            duplicate = con.execute(
                "SELECT appointment_id FROM appointments WHERE history_id=? AND appointment_date=? LIMIT 1",
                (history_id, appointment_date),
            ).fetchone()
            if duplicate:
                return jsonify(
                    ok=False,
                    error="Έχει γίνει ήδη καταχώριση παρουσίας για σήμερα.",
                    appointment_id=duplicate["appointment_id"],
                ), 409
            if not con.execute(
                "SELECT 1 FROM clinical_histories WHERE history_id=?", (history_id,),
            ).fetchone():
                return jsonify(ok=False, error="Το ιστορικό δεν βρέθηκε"), 404
            try:
                plan, referral, used_visits = validate_coverage(
                    con, history_id, payload.get("coverage_plan_id"),
                    payload.get("gesy_referral_id"), completing=True,
                )
                amount_due, copayment, effective_charge = financial_values_for_new_session(
                    con, plan, appointment_date, payload.get("copayment"),
                )
            except ValidationError as exc:
                return jsonify(ok=False, error=str(exc)), 400
            next_no = con.execute(
                "SELECT COALESCE(MAX(appointment_number),0)+1 FROM appointments WHERE history_id=?",
                (history_id,),
            ).fetchone()[0]
            cur = con.execute("""
                INSERT INTO appointments(
                    history_id, appointment_number, appointment_date, status, today,
                    coverage_plan_id, gesy_referral_id
                ) VALUES(?,?,?, 'completed', 0, ?, ?)
            """, (
                history_id, next_no, appointment_date, plan["coverage_plan_id"],
                referral["gesy_referral_id"] if referral else None,
            ))
            appointment_id = cur.lastrowid
            cur = con.execute("""
                INSERT INTO payments(
                    appointment_id, payment_date, amount_due, copayment,
                    amount_paid, receipt_amount
                ) VALUES(?,?,?,?,?,?)
            """, (appointment_id, appointment_date, amount_due, copayment, 0, 0))
            payment_id = cur.lastrowid
            log_change(
                con, app, "insert_appointment", "appointments", "appointment_id", appointment_id,
                None, None, {"appointment_id": appointment_id, "payment_id": payment_id},
                f"Δημιουργία συνεδρίας #{next_no} στο ιστορικό #{history_id}",
            )
        return jsonify(
            ok=True, appointment_id=appointment_id, payment_id=payment_id,
            appointment_number=next_no, appointment_date=appointment_date,
            amount_due=amount_due, effective_charge=effective_charge,
            copayment=copayment, amount_paid=0, receipt_amount=0,
            referral_exhausted=bool(
                referral and referral["allowed_visits"] is not None
                and used_visits + 1 == referral["allowed_visits"]
            ),
        )

    @app.post("/api/payment/ensure/<int:appointment_id>")
    def api_payment_ensure(appointment_id: int):
        with write_transaction(app) as con:
            row = con.execute(
                "SELECT payment_id FROM payments WHERE appointment_id=? ORDER BY payment_id LIMIT 1",
                (appointment_id,),
            ).fetchone()
            if row:
                return jsonify(ok=True, payment_id=row[0])
            appointment = con.execute(
                """SELECT history_id, appointment_number, appointment_date
                   FROM appointments WHERE appointment_id=?""",
                (appointment_id,),
            ).fetchone()
            if not appointment:
                return jsonify(ok=False, error="Η συνεδρία δεν βρέθηκε"), 404
            try:
                default_due = automatic_appointment_due(
                    app, con, appointment["history_id"],
                    before_appointment_number=appointment["appointment_number"],
                    before_appointment_id=appointment_id,
                )
            except (LookupError, ValidationError) as exc:
                return jsonify(ok=False, error=f"Δεν υπολογίστηκε η αυτόματη χρέωση: {exc}"), 400
            cur = con.execute("""
                INSERT INTO payments(appointment_id,payment_date,amount_due,amount_paid,receipt_amount)
                VALUES(?,?,?,?,?)
            """, (appointment_id, appointment["appointment_date"], default_due, 0, 0))
            payment_id = cur.lastrowid
            log_change(
                con, app, "insert", "payments", "payment_id", payment_id, None, None,
                {"payment_id": payment_id, "appointment_id": appointment_id},
                f"Δημιουργία οικονομικής εγγραφής συνεδρίας #{appointment_id}",
            )
        return jsonify(ok=True, payment_id=payment_id)

    @app.get("/api/undo/peek")
    def api_undo_peek():
        change = get_last_change(app)
        if not change:
            return jsonify(ok=False, error="Δεν υπάρχει αλλαγή για αναίρεση"), 404
        return jsonify(
            ok=True, description=change["description"] or "Τελευταία αλλαγή",
            created_at=change["created_at"],
        )

    @app.post("/api/undo")
    def api_undo():
        try:
            description = undo_last_change(app)
            return jsonify(ok=True, message=f"Αναιρέθηκε: {description}")
        except LookupError as exc:
            return jsonify(ok=False, error=str(exc)), 404
        except (ValidationError, sqlite3.Error) as exc:
            app.logger.exception("Αποτυχία αναίρεσης")
            return jsonify(ok=False, error=f"Αποτυχία αναίρεσης: {exc}"), 500

    return app


def json_payload() -> dict[str, Any]:
    if not request.is_json:
        abort(415, description="Απαιτείται αίτημα JSON")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="Το περιεχόμενο JSON δεν είναι έγκυρο")
    return payload


def validated_sort(
    sort_map: dict[str, str], default: str, default_dir: str = "asc"
) -> tuple[str, str]:
    sort_key = request.args.get("sort", default)
    if sort_key not in sort_map:
        sort_key = default
    direction = request.args.get("dir", default_dir).lower()
    if direction not in {"asc", "desc"}:
        direction = default_dir
    return sort_key, direction


def validated_sorts(
    sort_map: dict[str, str], default: str, default_dir: str = "asc",
) -> list[tuple[str, str]]:
    raw_keys = request.args.get("sort", "")
    raw_directions = request.args.get("dir", "")
    keys = [key.strip() for key in raw_keys.split(",") if key.strip()]
    directions = [direction.strip().lower() for direction in raw_directions.split(",")]
    sorts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, key in enumerate(keys):
        if key not in sort_map or key in seen:
            continue
        direction = directions[index] if index < len(directions) else "asc"
        if direction not in {"asc", "desc"}:
            direction = "asc"
        sorts.append((key, direction))
        seen.add(key)
    return sorts or [(default, default_dir)]


def batch_offset() -> int:
    return max(0, request.args.get("offset", 0, type=int) or 0)


def wants_list_batch() -> bool:
    return request.args.get("format") == "json"


def list_batch_response(
    template: str, rows: list[sqlite3.Row], offset: int, has_more: bool,
):
    return jsonify(
        ok=True,
        html=render_template(template, rows=rows),
        offset=offset,
        next_offset=offset + len(rows),
        count=len(rows),
        has_more=has_more,
    )


def add_normalized_search(
    where: list[str], params: list[Any], query: str, expressions: tuple[str, ...],
    prefix_expressions: tuple[str, ...] = (),
) -> None:
    terms = [term for term in normalize_search_text(query).split() if term]
    for term in terms:
        where.append("(" + " OR ".join(
            f"PYCASEFOLD(COALESCE({expr},'')) LIKE ? ESCAPE '\\'"
            for expr in expressions
        ) + ")")
        params.extend(
            [
                f"{escape_like(term)}%" if expr in prefix_expressions
                else f"%{escape_like(term)}%"
                for expr in expressions
            ]
        )


def query_count(
    app: Flask, from_sql: str, where: list[str], params: list[Any],
) -> int:
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    with db_conn(app) as con:
        return int(con.execute(
            f"SELECT COUNT(*) {from_sql}{where_sql}", params,
        ).fetchone()[0])


def batched_query(
    app: Flask, select_sql: str, from_sql: str, where: list[str], params: list[Any],
    order_sql: str, direction: str, offset: int, batch_size: int, tie_breaker: str,
) -> tuple[list[sqlite3.Row], bool]:
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    with db_conn(app) as con:
        rows = con.execute(
            f"{select_sql} {from_sql}{where_sql} "
            f"ORDER BY {order_sql} {direction.upper()}, {tie_breaker} LIMIT ? OFFSET ?",
            [*params, batch_size + 1, offset],
        ).fetchall()
    return rows[:batch_size], len(rows) > batch_size


def batched_query_multi(
    app: Flask, select_sql: str, from_sql: str, where: list[str], params: list[Any],
    order_terms: list[tuple[str, str]], offset: int, batch_size: int,
    tie_breaker: str,
) -> tuple[list[sqlite3.Row], bool]:
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    order_sql = ", ".join(
        f"{expression} {direction.upper()}"
        for expression, direction in order_terms
    )
    with db_conn(app) as con:
        rows = con.execute(
            f"{select_sql} {from_sql}{where_sql} "
            f"ORDER BY {order_sql}, {tie_breaker} LIMIT ? OFFSET ?",
            [*params, batch_size + 1, offset],
        ).fetchall()
    return rows[:batch_size], len(rows) > batch_size


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_lookup_text(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) > 500:
        raise ValidationError("Η τιμή αυτόματης συμπλήρωσης είναι υπερβολικά μεγάλη")
    return text


def values_differ_from_defaults(values: dict[str, Any], defaults: dict[str, Any]) -> bool:
    return any(
        str(value or "") != str(defaults.get(key, "") or "")
        for key, value in values.items()
    )


def resolve_related_choice(
    con: sqlite3.Connection, kind: str, raw_id: Any, raw_text: Any,
) -> tuple[int | None, dict[str, Any] | None]:
    configs = {
        "referral": {
            "table": "referrals", "pk": "referral_id",
            "display": "TRIM(COALESCE(last_name,'') || ' ' || COALESCE(first_name,''))",
            "insert": "INSERT INTO referrals(last_name) VALUES(?)",
        },
        "profession": {
            "table": "professions", "pk": "profession_id",
            "display": "profession_name",
            "insert": "INSERT INTO professions(profession_name) VALUES(?)",
        },
        "doctor": {
            "table": "doctors", "pk": "doctor_id",
            "display": "TRIM(COALESCE(last_name,'') || ' ' || COALESCE(first_name,''))",
            "insert": "INSERT INTO doctors(last_name) VALUES(?)",
        },
    }
    config = configs.get(kind)
    if not config:
        raise ValidationError("Μη έγκυρος τύπος συσχετισμένης τιμής")

    text = normalize_lookup_text(raw_text)
    choice_id: int | None = None
    if raw_id not in (None, ""):
        try:
            choice_id = int(str(raw_id))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Η επιλεγμένη πρόταση δεν είναι έγκυρη") from exc
        if choice_id <= 0:
            raise ValidationError("Η επιλεγμένη πρόταση δεν είναι έγκυρη")
        row = con.execute(
            f"SELECT {config['pk']} AS item_id, {config['display']} AS display_value "
            f"FROM {config['table']} WHERE {config['pk']}=?",
            (choice_id,),
        ).fetchone()
        if row and (not text or normalize_search_text(row["display_value"]) == normalize_search_text(text)):
            return row["item_id"], None

    if not text:
        return None, None

    existing = con.execute(
        f"SELECT {config['pk']} AS item_id FROM {config['table']} "
        f"WHERE PYCASEFOLD(TRIM({config['display']}))=? ORDER BY {config['pk']} LIMIT 1",
        (normalize_search_text(text),),
    ).fetchone()
    if existing:
        return existing["item_id"], None

    cursor = con.execute(config["insert"], (text,))
    new_id = cursor.lastrowid
    return new_id, {
        "table": config["table"], "pk_name": config["pk"], "pk_value": new_id,
    }


def automatic_appointment_due(
    app: Flask, con: sqlite3.Connection, history_id: int,
    *, before_appointment_number: int | None = None, before_appointment_id: int | None = None,
) -> float:
    history = con.execute(
        "SELECT social_security FROM clinical_histories WHERE history_id=?", (history_id,)
    ).fetchone()
    if not history:
        raise LookupError("Το ιστορικό δεν βρέθηκε")

    where = ["a.history_id=?", "pay.amount_due IS NOT NULL"]
    params: list[Any] = [history_id]
    if before_appointment_id is not None:
        if before_appointment_number is not None:
            where.append("""
                (a.appointment_number < ? OR
                 (a.appointment_number = ? AND a.appointment_id < ?))
            """)
            params.extend([
                before_appointment_number, before_appointment_number, before_appointment_id,
            ])
        else:
            where.append("a.appointment_id < ?")
            params.append(before_appointment_id)

    previous_due = con.execute(f"""
        SELECT pay.amount_due
        FROM appointments a
        JOIN payments pay ON pay.payment_id = (
            SELECT p2.payment_id FROM payments p2
            WHERE p2.appointment_id=a.appointment_id
            ORDER BY p2.payment_id LIMIT 1
        )
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(a.appointment_number, 0) DESC, a.appointment_id DESC
        LIMIT 1
    """, params).fetchone()
    if previous_due:
        return float(validate_default_amount(previous_due["amount_due"]))
    setting_key, fallback = (
        ("first_gesy_amount", "10.00")
        if normalize_search_text(history["social_security"]) == normalize_search_text("ΓΕΣΥ")
        else ("first_other_amount", "35.00")
    )
    return float(validate_default_amount(get_setting(app, setting_key, fallback)))


def describe_update(table: str, column: str, pk: int) -> str:
    object_labels = {
        "patients": "ασθενούς", "clinical_histories": "ιστορικού",
        "appointments": "συνεδρίας", "payments": "οικονομικής εγγραφής",
    }
    return f"Αλλαγή «{FIELD_LABELS.get(column, column)}» {object_labels.get(table, table)} #{pk}"
