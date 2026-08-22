from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "out" / "tracker.db"


def connect_tracker(path: Path = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
