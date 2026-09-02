"""Read-only, schema-specific calculations for the Statistics dashboard."""
from __future__ import annotations

from collections import Counter
from datetime import date
from statistics import median
from typing import Any


def _iso(value: str | None, label: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"Η τιμή «{label}» πρέπει να είναι έγκυρη ημερομηνία YYYY-MM-DD.") from exc


def parse_period(args: Any) -> dict[str, Any]:
    year = (args.get("year") or "").strip()
    start = _iso((args.get("from") or "").strip() or None, "Από ημερομηνία")
    end = _iso((args.get("to") or "").strip() or None, "Έως ημερομηνία")
    if year and (not year.isdigit() or not 1900 <= int(year) <= 9999):
        raise ValueError("Το έτος δεν είναι έγκυρο.")
    if start and end and start > end:
        raise ValueError("Η «Από ημερομηνία» πρέπει να προηγείται της «Έως ημερομηνία».")
    return {"year": int(year) if year else None, "from": start, "to": end}


def _date_clause(column: str, period: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses = [f"{column} IS NOT NULL", f"TRIM({column}) GLOB '????-??-??'", f"date({column}) IS NOT NULL"]
    params: list[Any] = []
    if period["year"]:
        clauses.append(f"substr(TRIM({column}), 1, 4)=?")
        params.append(f"{period['year']:04d}")
    if period["from"]:
        clauses.append(f"date({column})>=date(?)")
        params.append(period["from"])
    if period["to"]:
        clauses.append(f"date({column})<=date(?)")
        params.append(period["to"])
    return " AND ".join(clauses), params


def _series(rows: list[Any], months: bool) -> list[dict[str, Any]]:
    values = {str(row[0]): int(row[1]) for row in rows}
    if months:
        labels = ["Ιαν", "Φεβ", "Μαρ", "Απρ", "Μάι", "Ιουν", "Ιουλ", "Αυγ", "Σεπ", "Οκτ", "Νοε", "Δεκ"]
        return [{"label": labels[i - 1], "value": values.get(f"{i:02d}", 0)} for i in range(1, 13)]
    return [{"label": label, "value": value} for label, value in sorted(values.items())]


def _rank_limit(args: Any) -> int | None:
    return {"10": 10, "20": 20, "all": None}.get((args.get("top") or "10").strip(), 10)


def _rank(rows: list[Any], limit: int | None) -> list[dict[str, Any]]:
    values = [dict(row) for row in rows]
    return values if limit is None else values[:limit]


def _bucket(value: int, ranges: list[tuple[str, int, int | None]]) -> str:
    for label, low, high in ranges:
        if value >= low and (high is None or value <= high):
            return label
    return ranges[-1][0]


def build_statistics(con: Any, args: Any) -> dict[str, Any]:
    """Return aggregates only; this function performs no writes and exposes no PII."""
    period, limit = parse_period(args), _rank_limit(args)
    appointment_where, appointment_params = _date_clause("a.appointment_date", period)
    visit_cte = f"""WITH valid_visits AS (
      SELECT a.appointment_id, a.history_id, date(a.appointment_date) AS visit_date
      FROM appointments a JOIN clinical_histories h ON h.history_id=a.history_id WHERE {appointment_where})"""
    visits_total = con.execute(visit_cte + " SELECT COUNT(*) FROM valid_visits", appointment_params).fetchone()[0]
    by_month = period["year"] is not None
    group = "strftime('%m', visit_date)" if by_month else "strftime('%Y', visit_date)"
    visits_series = _series(con.execute(visit_cte + f" SELECT {group}, COUNT(*) FROM valid_visits GROUP BY 1", appointment_params).fetchall(), by_month)

    first_visits = """WITH first_visits AS (
      SELECT h.patient_id, MIN(date(a.appointment_date)) AS first_date FROM appointments a
      JOIN clinical_histories h ON h.history_id=a.history_id
      WHERE a.appointment_date IS NOT NULL AND TRIM(a.appointment_date) GLOB '????-??-??' AND date(a.appointment_date) IS NOT NULL GROUP BY h.patient_id)"""
    first_where, first_params = _date_clause("first_date", period)
    new_patients_total = con.execute(first_visits + f" SELECT COUNT(*) FROM first_visits WHERE {first_where}", first_params).fetchone()[0]
    group = "strftime('%m', first_date)" if by_month else "strftime('%Y', first_date)"
    new_patients_series = _series(con.execute(first_visits + f" SELECT {group}, COUNT(*) FROM first_visits WHERE {first_where} GROUP BY 1", first_params).fetchall(), by_month)

    starts_cte = """WITH first_history_visits AS (
      SELECT history_id, MIN(date(appointment_date)) AS first_visit_date FROM appointments
      WHERE appointment_date IS NOT NULL AND TRIM(appointment_date) GLOB '????-??-??' AND date(appointment_date) IS NOT NULL GROUP BY history_id),
    history_starts AS (
      SELECT h.history_id, h.patient_id, CASE WHEN h.history_date IS NOT NULL AND TRIM(h.history_date) GLOB '????-??-??' AND date(h.history_date) IS NOT NULL THEN date(h.history_date) ELSE f.first_visit_date END AS history_start
      FROM clinical_histories h LEFT JOIN first_history_visits f ON f.history_id=h.history_id)"""
    history_where, history_params = _date_clause("history_start", period)
    histories_total = con.execute(starts_cte + f" SELECT COUNT(*) FROM history_starts WHERE history_start IS NOT NULL AND {history_where}", history_params).fetchone()[0]
    group = "strftime('%m', history_start)" if by_month else "strftime('%Y', history_start)"
    histories_series = _series(con.execute(starts_cte + f" SELECT {group}, COUNT(*) FROM history_starts WHERE history_start IS NOT NULL AND {history_where} GROUP BY 1", history_params).fetchall(), by_month)

    scoped_cte = starts_cte + f""", scoped_histories AS (
      SELECT hs.history_id, hs.patient_id, hs.history_start, h.main_diagnosis, h.body_area, h.doctor_id
      FROM history_starts hs JOIN clinical_histories h ON h.history_id=hs.history_id
      WHERE hs.history_start IS NOT NULL AND {history_where}), scoped_visits AS (
      SELECT a.appointment_id, a.history_id FROM appointments a JOIN scoped_histories sh ON sh.history_id=a.history_id WHERE {appointment_where})"""
    scoped_params = history_params + appointment_params
    def ranking(field: str) -> list[dict[str, Any]]:
        rows = con.execute(scoped_cte + f""" SELECT TRIM(sh.{field}) AS name, COUNT(DISTINCT sh.history_id) AS histories, COUNT(DISTINCT sv.appointment_id) AS visits
          FROM scoped_histories sh LEFT JOIN scoped_visits sv ON sv.history_id=sh.history_id
          WHERE NULLIF(TRIM(COALESCE(sh.{field},'')),'') IS NOT NULL GROUP BY TRIM(sh.{field}) ORDER BY histories DESC, visits DESC, name""", scoped_params).fetchall()
        return _rank(rows, limit)
    diagnoses, body_areas = ranking("main_diagnosis"), ranking("body_area")
    referral_rows = con.execute(scoped_cte + """ SELECT TRIM(COALESCE(r.last_name,'') || CASE WHEN NULLIF(TRIM(r.first_name),'') IS NULL THEN '' ELSE ' ' || TRIM(r.first_name) END) AS name, COUNT(DISTINCT sh.patient_id) AS patients, COUNT(DISTINCT sh.history_id) AS histories, COUNT(DISTINCT sv.appointment_id) AS visits
      FROM scoped_histories sh JOIN patients p ON p.patient_id=sh.patient_id JOIN referrals r ON r.referral_id=p.referral_id LEFT JOIN scoped_visits sv ON sv.history_id=sh.history_id
      WHERE NULLIF(TRIM(COALESCE(r.last_name,'') || COALESCE(r.first_name,'')),'') IS NOT NULL GROUP BY r.referral_id ORDER BY patients DESC, histories DESC, visits DESC, name""", scoped_params).fetchall()
    doctor_rows = con.execute(scoped_cte + """ SELECT TRIM(COALESCE(d.last_name,'') || CASE WHEN NULLIF(TRIM(d.first_name),'') IS NULL THEN '' ELSE ' ' || TRIM(d.first_name) END) AS name, COUNT(DISTINCT sh.patient_id) AS patients, COUNT(DISTINCT sh.history_id) AS histories, COUNT(DISTINCT sv.appointment_id) AS visits
      FROM scoped_histories sh JOIN doctors d ON d.doctor_id=sh.doctor_id LEFT JOIN scoped_visits sv ON sv.history_id=sh.history_id
      WHERE NULLIF(TRIM(COALESCE(d.last_name,'') || COALESCE(d.first_name,'')),'') IS NOT NULL GROUP BY d.doctor_id ORDER BY patients DESC, histories DESC, visits DESC, name""", scoped_params).fetchall()
    doctors = _rank(doctor_rows, limit)
    for item in doctors: item["average_visits"] = round(item["visits"] / item["histories"], 2) if item["histories"] else 0

    population_cte = scoped_cte + ", selected_patients AS (SELECT patient_id, MIN(history_start) AS reference_date FROM scoped_histories GROUP BY patient_id)"
    genders = {"Άνδρες": 0, "Γυναίκες": 0, "Άγνωστο": 0}; ages = {key: 0 for key in ("0–17", "18–29", "30–39", "40–49", "50–59", "60–69", "70–79", "80+", "Άγνωστο")}
    age_ranges = [("0–17",0,17),("18–29",18,29),("30–39",30,39),("40–49",40,49),("50–59",50,59),("60–69",60,69),("70–79",70,79),("80+",80,None)]
    for row in con.execute(population_cte + " SELECT p.gender,p.birthdate,sp.reference_date FROM selected_patients sp JOIN patients p ON p.patient_id=sp.patient_id", scoped_params):
        gender = (row[0] or "").strip().casefold(); genders["Άνδρες" if gender in {"male","m","άνδρας","ανδρας"} else "Γυναίκες" if gender in {"female","f","γυναίκα","γυναικα"} else "Άγνωστο"] += 1
        try:
            born, ref = date.fromisoformat((row[1] or "").strip()), date.fromisoformat(row[2]); age = ref.year-born.year-((ref.month,ref.day)<(born.month,born.day))
            if not 0 <= age <= 120: raise ValueError
            ages[_bucket(age, age_ranges)] += 1
        except (ValueError, TypeError): ages["Άγνωστο"] += 1
    visit_counts = [row[0] for row in con.execute(scoped_cte + " SELECT COUNT(sv.appointment_id) FROM scoped_histories sh LEFT JOIN scoped_visits sv ON sv.history_id=sh.history_id GROUP BY sh.history_id", scoped_params)]
    visit_labels = [("1 επίσκεψη",1,1),("2–3",2,3),("4–5",4,5),("6–10",6,10),("11–15",11,15),("16+",16,None)]; visit_dist = Counter(_bucket(n,visit_labels) for n in visit_counts if n > 0)
    conditions: dict[str,list[int]] = {}
    for row in con.execute(scoped_cte + " SELECT TRIM(sh.main_diagnosis),COUNT(sv.appointment_id) FROM scoped_histories sh LEFT JOIN scoped_visits sv ON sv.history_id=sh.history_id WHERE NULLIF(TRIM(COALESCE(sh.main_diagnosis,'')),'') IS NOT NULL GROUP BY sh.history_id,TRIM(sh.main_diagnosis)", scoped_params): conditions.setdefault(row[0],[]).append(row[1])
    condition_visits = [{"name":k,"histories":len(v),"visits":sum(v),"average_visits":round(sum(v)/len(v),2),"median_visits":median(v)} for k,v in conditions.items()]
    order = "visits" if (args.get("condition_sort") or "") == "visits" else "histories"; condition_visits.sort(key=lambda x:(-x[order],-x["visits"],x["name"]))
    duration_rows = con.execute(starts_cte + f""" SELECT h.main_diagnosis,h.history_id,MIN(date(a.appointment_date)),MAX(date(a.appointment_date)) FROM history_starts hs JOIN clinical_histories h ON h.history_id=hs.history_id JOIN appointments a ON a.history_id=h.history_id WHERE hs.history_start IS NOT NULL AND {history_where} AND a.appointment_date IS NOT NULL AND TRIM(a.appointment_date) GLOB '????-??-??' AND date(a.appointment_date) IS NOT NULL GROUP BY h.history_id""", history_params).fetchall()
    durations = [(date.fromisoformat(r[3])-date.fromisoformat(r[2])).days for r in duration_rows]; duration_labels=[("0 ημέρες",0,0),("1–7",1,7),("8–14",8,14),("15–30",15,30),("31–60",31,60),("61–90",61,90),("90+",91,None)]; duration_dist=Counter(_bucket(n,duration_labels) for n in durations)
    duration_by_condition: dict[str,list[int]] = {}
    for row,days in zip(duration_rows,durations):
        if (row[0] or "").strip(): duration_by_condition.setdefault(row[0].strip(),[]).append(days)
    returning_rows = con.execute(starts_cte + f" SELECT patient_id,COUNT(*),GROUP_CONCAT(history_start,'|') FROM history_starts WHERE history_start IS NOT NULL AND {history_where} GROUP BY patient_id", history_params).fetchall()
    return_labels=[("1 ιστορικό",1,1),("2 ιστορικά",2,2),("3 ιστορικά",3,3),("4–5 ιστορικά",4,5),("6+",6,None)]; returning_dist=Counter(_bucket(r[1],return_labels) for r in returning_rows); total_people=len(returning_rows); returning=sum(1 for r in returning_rows if r[1]>=2)
    intervals=[]
    for row in returning_rows:
        starts=sorted(date.fromisoformat(item) for item in (row[2] or '').split('|') if item)
        intervals.extend((later-earlier).days for earlier,later in zip(starts,starts[1:]))
    return {"period":period,"top":args.get("top") or "10","condition_sort":order,"overview":{"visits":visits_total,"new_patients":new_patients_total,"new_histories":histories_total},"visits_series":visits_series,"new_patients_series":new_patients_series,"histories_series":histories_series,"diagnoses":diagnoses,"body_areas":body_areas,"referrals":_rank(referral_rows,limit),"doctors":doctors,"genders":[{"label":k,"value":v} for k,v in genders.items()],"ages":[{"label":k,"value":v} for k,v in ages.items()],"visit_summary":{"average":round(sum(visit_counts)/len(visit_counts),2) if visit_counts else 0,"median":median(visit_counts) if visit_counts else 0,"minimum":min(visit_counts) if visit_counts else 0,"maximum":max(visit_counts) if visit_counts else 0},"visit_distribution":[{"label":k,"value":visit_dist[k]} for k,_,_ in visit_labels],"condition_visits":condition_visits if limit is None else condition_visits[:limit],"duration_summary":{"average":round(sum(durations)/len(durations),2) if durations else 0,"median":median(durations) if durations else 0,"minimum":min(durations) if durations else 0,"maximum":max(durations) if durations else 0},"duration_distribution":[{"label":k,"value":duration_dist[k]} for k,_,_ in duration_labels],"duration_by_condition":sorted(({"name":k,"average_days":round(sum(v)/len(v),1)} for k,v in duration_by_condition.items()),key=lambda x:(-x["average_days"],x["name"]))[:10],"returning":{"total":total_people,"one_history_percent":round((total_people-returning)*100/total_people,1) if total_people else 0,"returning_percent":round(returning*100/total_people,1) if total_people else 0,"average_interval":round(sum(intervals)/len(intervals),1) if intervals else None},"returning_distribution":[{"label":k,"value":returning_dist[k]} for k,_,_ in return_labels]}
