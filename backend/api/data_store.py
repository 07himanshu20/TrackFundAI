"""
data_store.py
In-memory cache for parsed Excel MIS data.
Loaded once at Django startup (or on new file upload).
"""
import logging
import os
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_store = {
    "data": None,          # parsed dict from excel_parser.parse_excel()
    "filepath": None,      # absolute path of the currently loaded file
    "loaded_at": None,     # ISO timestamp
}


def load_from_env():
    """Load the MIS file whose path is set in .env (MIS_FILE_PATH)."""
    path = getattr(settings, "MIS_FILE_PATH", "")
    if path and os.path.isfile(path):
        load_file(path)
    else:
        if path:
            logger.warning("MIS_FILE_PATH set but file not found: %s", path)
        else:
            logger.info("MIS_FILE_PATH not configured — waiting for upload.")


def load_file(filepath: str) -> dict:
    """Parse the given Excel file and cache the result."""
    from api.excel_parser import parse_excel
    from datetime import datetime, timezone

    logger.info("Parsing MIS file: %s", filepath)
    data = parse_excel(filepath)

    with _lock:
        _store["data"] = data
        _store["filepath"] = filepath
        _store["loaded_at"] = datetime.now(timezone.utc).isoformat()

    logger.info("MIS file parsed OK. Errors: %s", data.get("parse_report", {}).get("errors", []))
    return data


def get_data() -> dict | None:
    """Return the cached data or None if not yet loaded."""
    with _lock:
        return _store["data"]


def get_meta() -> dict:
    """Return metadata about the currently loaded file."""
    with _lock:
        return {
            "filepath": _store["filepath"],
            "loaded_at": _store["loaded_at"],
            "has_data": _store["data"] is not None,
        }
