"""One-off + reusable: patch submit.py with liveness precheck, sweep dead postings."""
from pathlib import Path
import ast
import sqlite3
import urllib.request

ROOT = Path(__file__).resolve().parent

p = ROOT / "submit.py"
s = p.read_text()

if "_posting_dead" not in s:
    s = s.replace('''        pdf = Path(r["resume_pdf"])
        if not pdf.is_absolute():
            pdf = ROOT / pdf
        try:
            res = fn(r["url"], pdf, slug, dry_run=dry_run)''',
'''        pdf = Path(r["resume_pdf"])
        if not pdf.is_absolute():
            pdf = ROOT / pdf
        if _posting_dead(r["url"]):
            conn.execute("UPDATE postings SET status='filtered_out' WHERE posting_id=?",
                         (r["posting_id"],))
            conn.commit()
            results.append({"company": r["company"], "ats": ats, "status": "dead posting"})
            continue
        try:
            res = fn(r["url"], pdf, slug, dry_run=dry_run)''')

    guard = '''DEAD_MARKERS = ("job not found", "no longer available", "job you requested was not found",
                "position has been filled", "posting is closed", "job posting is no longer")


def _posting_dead(url: str) -> bool:
    """Cheap liveness sniff before spending a browser session."""
    import urllib.request as _ur
    try:
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=15) as r:
            body = r.read(60000).decode("utf-8", "replace").lower()
        return any(m in body for m in DEAD_MARKERS)
    except Exception:
        return False


'''
    anchor = "def submit_ready("
    s = s.replace(anchor, guard + anchor, 1)
    p.write_text(s)
    ast.parse(s)
    print("liveness precheck added")
else:
    print("precheck already present")

# sweep current failed/manual rows on http-checkable ATSes
conn = sqlite3.connect(ROOT / "out" / "tracker.db")
DEAD = ("job not found", "no longer available", "was not found", "posting is closed")
rows = conn.execute(
    "SELECT posting_id, company, url FROM postings WHERE status IN ('failed','manual') "
    "AND (url LIKE '%ashbyhq%' OR url LIKE '%greenhouse%' OR url LIKE '%lever%')").fetchall()
for pid, comp, url in rows:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        body = urllib.request.urlopen(req, timeout=15).read(60000).decode("utf-8", "replace").lower()
        if any(m in body for m in DEAD):
            conn.execute("UPDATE postings SET status='filtered_out' WHERE posting_id=?", (pid,))
            print("dead:", comp)
        else:
            print("alive:", comp)
    except Exception as e:
        print("check failed:", comp, str(e)[:40])
conn.commit()
