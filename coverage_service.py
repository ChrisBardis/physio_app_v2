from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime
from typing import Any

from physio_core import ValidationError


FIRST_AUTOMATIC_GESY_MONTH = (2026, 9)


def _nonnegative_money(value: Any, label: str, *, required: bool) -> float | None:
    if value in (None, ""):
        if required:
            raise ValidationError(f"Το πεδίο «{label}» είναι υποχρεωτικό")
        return None
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Το πεδίο «{label}» δεν είναι έγκυρο ποσό") from exc
    if not math.isfinite(number) or number < 0:
        raise ValidationError(f"Το πεδίο «{label}» πρέπει να είναι μη αρνητικό")
    return round(number, 2)


def parse_positive_int(value: Any, label: str, *, required: bool = True) -> int | None:
    if value in (None, ""):
        if required:
            raise ValidationError(f"Το πεδίο «{label}» είναι υποχρεωτικό")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Το πεδίο «{label}» δεν είναι έγκυρο") from exc
    if number <= 0:
        raise ValidationError(f"Το πεδίο «{label}» πρέπει να είναι θετικός αριθμός")
    return number


def parse_iso_date(value: Any) -> date:
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    raise ValidationError("Η ημερομηνία συνεδρίας δεν είναι έγκυρη")


def ensure_gesy_month(con: sqlite3.Connection, appointment_date: Any) -> float:
    """Return a GESY rate, carrying forward only from September 2026 onward."""
    target = parse_iso_date(appointment_date)
    target_key = (target.year, target.month)
    exact = con.execute(
        "SELECT rate FROM GesyMonth WHERE year=? AND month=?", target_key,
    ).fetchone()
    if exact:
        return float(exact["rate"])
    if target_key < FIRST_AUTOMATIC_GESY_MONTH:
        raise ValidationError(
            "Δεν υπάρχει καταχωρημένο ΓεΣΥ Rate για τον μήνα της συνεδρίας"
        )

    latest = con.execute("""
        SELECT year, month, rate FROM GesyMonth
        WHERE year < ? OR (year=? AND month < ?)
        ORDER BY year DESC, month DESC LIMIT 1
    """, (target.year, target.year, target.month)).fetchone()
    if not latest or (latest["year"], latest["month"]) < FIRST_AUTOMATIC_GESY_MONTH:
        raise ValidationError("Δεν υπάρχει προηγούμενο γνωστό ΓεΣΥ Rate")

    year, month, rate = int(latest["year"]), int(latest["month"]), float(latest["rate"])
    while (year, month) < target_key:
        month += 1
        if month == 13:
            year += 1
            month = 1
        con.execute(
            "INSERT OR IGNORE INTO GesyMonth(year,month,rate) VALUES(?,?,?)",
            (year, month, rate),
        )
        row = con.execute(
            "SELECT rate FROM GesyMonth WHERE year=? AND month=?", (year, month),
        ).fetchone()
        rate = float(row["rate"])
    return rate


def coverage_plans(con: sqlite3.Connection, *, active_only: bool = True) -> list[sqlite3.Row]:
    where = "WHERE active=1" if active_only else ""
    return con.execute(f"""
        SELECT coverage_plan_id, code, coverage_type, name, default_charge, active
        FROM CoveragePlans {where}
        ORDER BY CASE coverage_type WHEN 'GESY' THEN 0 WHEN 'SELF_PAY' THEN 1 ELSE 2 END,
                 name, coverage_plan_id
    """).fetchall()


def referral_rows(con: sqlite3.Connection, history_id: int) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT r.gesy_referral_id, r.history_id, r.referral_number,
               r.allowed_visits, r.notes,
               COUNT(CASE WHEN a.status='completed' THEN 1 END) AS used_visits
        FROM GesyReferrals r
        LEFT JOIN appointments a ON a.gesy_referral_id=r.gesy_referral_id
        WHERE r.history_id=?
        GROUP BY r.gesy_referral_id
        ORDER BY r.gesy_referral_id DESC
    """, (history_id,)).fetchall()


def validate_coverage(
    con: sqlite3.Connection,
    history_id: int,
    coverage_plan_id: Any,
    gesy_referral_id: Any,
    *,
    completing: bool = False,
    exclude_appointment_id: int | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row | None, int | None]:
    try:
        plan_id = int(coverage_plan_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Επιλέξτε έγκυρο πλάνο κάλυψης") from exc
    plan = con.execute("""
        SELECT * FROM CoveragePlans WHERE coverage_plan_id=? AND active=1
    """, (plan_id,)).fetchone()
    if not plan:
        raise ValidationError("Το πλάνο κάλυψης δεν είναι ενεργό ή δεν υπάρχει")

    if plan["coverage_type"] != "GESY":
        if gesy_referral_id not in (None, "", 0, "0"):
            raise ValidationError("Μη-ΓεΣΥ συνεδρία δεν μπορεί να έχει παραπεμπτικό ΓεΣΥ")
        if plan["default_charge"] is None:
            raise ValidationError("Το πλάνο δεν έχει προεπιλεγμένη χρέωση")
        return plan, None, None

    try:
        referral_id = int(gesy_referral_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Η συνεδρία ΓεΣΥ απαιτεί παραπεμπτικό") from exc
    referral = con.execute("""
        SELECT * FROM GesyReferrals
        WHERE gesy_referral_id=? AND history_id=?
    """, (referral_id, history_id)).fetchone()
    if not referral:
        raise ValidationError("Το παραπεμπτικό ΓεΣΥ δεν ανήκει σε αυτό το ιστορικό")

    used = con.execute("""
        SELECT COUNT(*) FROM appointments
        WHERE gesy_referral_id=? AND status='completed'
          AND (? IS NULL OR appointment_id<>?)
    """, (referral_id, exclude_appointment_id, exclude_appointment_id)).fetchone()[0]
    allowed = referral["allowed_visits"]
    if completing and allowed is not None and used >= allowed:
        raise ValidationError(
            f"Το παραπεμπτικό έχει εξαντληθεί ({used}/{allowed} επισκέψεις)"
        )
    return plan, referral, int(used)


def financial_values_for_new_session(
    con: sqlite3.Connection,
    plan: sqlite3.Row,
    appointment_date: Any,
    copayment: Any,
) -> tuple[float | None, float | None, float]:
    if plan["coverage_type"] == "GESY":
        rate = ensure_gesy_month(con, appointment_date)
        co = _nonnegative_money(copayment, "Συμπληρωμή", required=True)
        if co > rate:
            raise ValidationError("Η συμπληρωμή δεν μπορεί να υπερβαίνει τη χρέωση ΓεΣΥ")
        return None, co, rate
    charge = _nonnegative_money(plan["default_charge"], "Χρέωση", required=True)
    if copayment not in (None, ""):
        raise ValidationError("Η συμπληρωμή χρησιμοποιείται μόνο σε συνεδρίες ΓεΣΥ")
    return charge, None, charge


def effective_charge_sql(appointment_alias: str = "a", payment_alias: str = "pay") -> str:
    return f"""CASE
        WHEN cp.coverage_type='GESY' THEN gm.rate
        ELSE {payment_alias}.amount_due
    END"""

