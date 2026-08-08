#!/usr/bin/env python3
"""Run retailor_one.py for every pending (tailored/ready) posting except Rivian
(already deep-tailored + approved-pending on its thread). Sequential; logs a
summary line per posting."""
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

conn = sqlite3.connect(ROOT / "out" / "tracker.db")
rows = [r[0] for r in conn.execute(
    "SELECT p.posting_id FROM postings p JOIN emails e USING(posting_id) "
    "WHERE p.status IN ('tailored','ready')")]
conn.close()

print(f"retailoring {len(rows)} postings", flush=True)
fails = 0
for i, pid in enumerate(rows, 1):
    print(f"[{i}/{len(rows)}] {pid[:60]}", flush=True)
    p = subprocess.run([sys.executable, str(ROOT / "retailor_one.py"), pid])
    if p.returncode != 0:
        fails += 1
print(f"done: {len(rows) - fails} ok, {fails} failed", flush=True)
