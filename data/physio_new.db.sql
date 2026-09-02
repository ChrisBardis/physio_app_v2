BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS "Future_appointments" (
	"future_appointment_id"	INTEGER,
	"patient_id"	INTEGER NOT NULL,
	"history_id"	INTEGER NOT NULL,
	"appointment_date"	TEXT NOT NULL,
	"start_time"	TEXT NOT NULL,
	"duration_minutes"	INTEGER NOT NULL CHECK("duration_minutes" > 0),
	"status"	TEXT NOT NULL DEFAULT 'scheduled' CHECK("status" IN ('scheduled', 'completed', 'cancelled', 'no_show')),
	"notes"	TEXT,
	"completed_appointment_id"	INTEGER,
	"created_at"	TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("future_appointment_id" AUTOINCREMENT),
	FOREIGN KEY("completed_appointment_id") REFERENCES "appointments"("appointment_id"),
	FOREIGN KEY("history_id") REFERENCES "clinical_histories"("history_id"),
	FOREIGN KEY("patient_id") REFERENCES "patients"("patient_id")
);
CREATE TABLE IF NOT EXISTS "appointments" (
	"appointment_id"	INTEGER,
	"history_id"	INTEGER NOT NULL,
	"appointment_number"	INTEGER,
	"appointment_date"	TEXT,
	"appointment_time"	TEXT,
	"status"	TEXT DEFAULT 'completed',
	"notes"	TEXT,
	"today"	INTEGER DEFAULT 0,
	"created_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("appointment_id"),
	FOREIGN KEY("history_id") REFERENCES "clinical_histories"("history_id")
);
CREATE TABLE IF NOT EXISTS "clinical_histories" (
	"history_id"	INTEGER,
	"patient_id"	INTEGER NOT NULL,
	"history_date"	TEXT,
	"problem_description"	TEXT,
	"main_diagnosis"	TEXT,
	"date_completed"	TEXT,
	"is_active"	INTEGER DEFAULT 0,
	"doctor_id"	INTEGER,
	"social_security"	TEXT,
	"body_area"	TEXT,
	"for_print"	INTEGER DEFAULT 0,
	"for_xrays"	TEXT,
	"for_exercise"	TEXT,
	"today"	INTEGER DEFAULT 0,
	"icd10_code"	TEXT,
	"gesy_referral"	TEXT,
	"created_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("history_id"),
	FOREIGN KEY("doctor_id") REFERENCES "doctors"("doctor_id"),
	FOREIGN KEY("patient_id") REFERENCES "patients"("patient_id")
);
CREATE TABLE IF NOT EXISTS "doctors" (
	"doctor_id"	INTEGER,
	"first_name"	TEXT,
	"last_name"	TEXT,
	"specialty"	TEXT,
	"work_phone"	TEXT,
	"home_phone"	TEXT,
	"mobile_phone"	TEXT,
	"email"	TEXT,
	"notes"	TEXT,
	"created_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("doctor_id")
);
CREATE TABLE IF NOT EXISTS "patients" (
	"patient_id"	INTEGER,
	"first_name"	TEXT,
	"last_name"	TEXT,
	"gender"	TEXT,
	"mobile_phone"	TEXT,
	"home_phone"	TEXT,
	"work_phone"	TEXT,
	"email"	TEXT,
	"birthdate"	TEXT,
	"identity_number"	TEXT,
	"address"	TEXT,
	"city"	TEXT,
	"postal_code"	TEXT,
	"referral_id"	INTEGER,
	"profession_id"	INTEGER,
	"notes"	TEXT,
	"photo_path"	TEXT,
	"is_active"	INTEGER DEFAULT 0,
	"created_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("patient_id"),
	FOREIGN KEY("profession_id") REFERENCES "professions"("profession_id"),
	FOREIGN KEY("referral_id") REFERENCES "referrals"("referral_id")
);
CREATE TABLE IF NOT EXISTS "payments" (
	"payment_id"	INTEGER,
	"appointment_id"	INTEGER NOT NULL,
	"payment_date"	TEXT,
	"amount_due"	REAL DEFAULT 0,
	"amount_paid"	REAL DEFAULT 0,
	"receipt_amount"	TEXT,
	"payment_method"	TEXT,
	"notes"	TEXT,
	"created_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("payment_id" AUTOINCREMENT),
	FOREIGN KEY("appointment_id") REFERENCES "appointments"("appointment_id")
);
CREATE TABLE IF NOT EXISTS "professions" (
	"profession_id"	INTEGER,
	"profession_name"	TEXT NOT NULL,
	"profession_category"	TEXT,
	"notes"	TEXT,
	"created_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("profession_id" AUTOINCREMENT)
);
CREATE TABLE IF NOT EXISTS "referrals" (
	"referral_id"	INTEGER,
	"first_name"	TEXT,
	"last_name"	TEXT,
	"address"	TEXT,
	"work_phone"	TEXT,
	"mobile_phone"	TEXT,
	"notes"	TEXT,
	"created_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	"updated_at"	TEXT DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY("referral_id")
);
CREATE INDEX IF NOT EXISTS "idx_appointments_date" ON "appointments" (
	"appointment_date"
);
CREATE INDEX IF NOT EXISTS "idx_appointments_history" ON "appointments" (
	"history_id"
);
CREATE INDEX IF NOT EXISTS "idx_future_appointments_date" ON "Future_appointments" (
	"appointment_date"
);
CREATE INDEX IF NOT EXISTS "idx_future_appointments_history" ON "Future_appointments" (
	"history_id"
);
CREATE INDEX IF NOT EXISTS "idx_future_appointments_patient_date" ON "Future_appointments" (
	"patient_id",
	"appointment_date"
);
CREATE INDEX IF NOT EXISTS "idx_future_appointments_status" ON "Future_appointments" (
	"status"
);
CREATE INDEX IF NOT EXISTS "idx_histories_active" ON "clinical_histories" (
	"is_active"
);
CREATE INDEX IF NOT EXISTS "idx_histories_patient" ON "clinical_histories" (
	"patient_id"
);
CREATE INDEX IF NOT EXISTS "idx_patients_active" ON "patients" (
	"is_active"
);
CREATE INDEX IF NOT EXISTS "idx_patients_name" ON "patients" (
	"last_name",
	"first_name"
);
CREATE INDEX IF NOT EXISTS "idx_payments_appointment" ON "payments" (
	"appointment_id"
);
COMMIT;
