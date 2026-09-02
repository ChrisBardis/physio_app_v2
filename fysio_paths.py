from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


APP_NAME = "Fysio"
SOURCE_ROOT = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(
    getattr(sys, "_MEIPASS", SOURCE_ROOT) if FROZEN else SOURCE_ROOT
).resolve()

if FROZEN:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("Δεν βρέθηκε ο φάκελος LOCALAPPDATA των Windows")
    APP_ROOT = (Path(local_app_data) / APP_NAME).resolve()
else:
    APP_ROOT = SOURCE_ROOT

DATA_DIR = APP_ROOT / "data"
ARCHIVE_DIR = APP_ROOT / "archive"
LOG_DIR = APP_ROOT / "logs"
DEFAULT_BACKUP_DIR = (
    APP_ROOT / "backups" if FROZEN else SOURCE_ROOT.parent / "physio_backups"
)

DEFAULT_DB = DATA_DIR / "physio_new.db"
DEFAULT_META_DB = DATA_DIR / "physio_app_meta.db"
DEFAULT_DATABASE_SELECTION = DATA_DIR / "physio_database.json"

TEMPLATE_DIR = RESOURCE_ROOT / "templates"
STATIC_DIR = RESOURCE_ROOT / "static"
PACKAGED_DEMO_DB = (
    RESOURCE_ROOT / "resources" / "physio_new.db"
    if FROZEN
    else SOURCE_ROOT / "data" / "physio_new.db"
)


def prepare_writable_layout() -> None:
    """Create writable folders and provision the packaged Demo once.

    Existing databases and user files are never overwritten. In development
    mode the established project paths remain unchanged.
    """
    for directory in (DATA_DIR, DEFAULT_BACKUP_DIR, ARCHIVE_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    if not FROZEN or DEFAULT_DB.exists():
        return
    if not PACKAGED_DEMO_DB.is_file():
        raise RuntimeError("Δεν βρέθηκε η ενσωματωμένη Demo βάση")

    temporary = DEFAULT_DB.with_name(f".{DEFAULT_DB.name}.{os.getpid()}.tmp")
    try:
        with PACKAGED_DEMO_DB.open("rb") as source, temporary.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        try:
            temporary.rename(DEFAULT_DB)
        except FileExistsError:
            # Another correctly synchronized startup provisioned it first.
            pass
    finally:
        temporary.unlink(missing_ok=True)
