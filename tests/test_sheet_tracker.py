import json
import sqlite3

import sheet_tracker
from submission.attempts import ensure_submission_attempts


def test_only_real_responses_count_as_heard_back():
    assert sheet_tracker._is_response_event("offer") is True
    assert sheet_tracker._is_response_event("interview_invite") is True
    assert sheet_tracker._is_response_event("oa_invite") is True
    assert sheet_tracker._is_response_event("recruiter_reply") is True
    assert sheet_tracker._is_response_event("rejection") is True
    assert sheet_tracker._is_response_event("confirmation") is False
    assert sheet_tracker._is_response_event("application_received") is False


def _manual_actions_db(tmp_path):
    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE postings (
            posting_id TEXT PRIMARY KEY,
            company TEXT,
            title TEXT,
            source TEXT,
            url TEXT,
            status TEXT,
            last_error TEXT,
            first_seen INTEGER,
            last_attempt_at INTEGER,
            outcome TEXT
        )
        """
    )
    conn.execute("CREATE TABLE emails (posting_id TEXT, resume_path TEXT)")
    ensure_submission_attempts(conn)
    conn.executemany(
        "INSERT INTO postings VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "captcha",
                "Captcha Co",
                "SWE",
                "lever",
                "https://captcha.example/job",
                "manual",
                "Lever hCaptcha requires manual completion raw answer: yes /Users/david/local.png",
                100,
                180,
                "",
            ),
            (
                "uncertain",
                "Uncertain Co",
                "Platform Eng",
                "greenhouse",
                "https://uncertain.example/job",
                "manual",
                "submit clicked but confirmation was not observed; /Users/david/raw-answer.json",
                120,
                190,
                "",
            ),
            (
                "debt",
                "Debt Co",
                "Backend",
                "other",
                "https://debt.example/job",
                "manual",
                "no adapter for other",
                130,
                195,
                "",
            ),
            (
                "ashby-spam",
                "Spam Co",
                "ML Engineer",
                "ashby",
                "https://spam.example/job",
                "manual",
                "Ashby rejected the submission as possible spam: use normal Chrome",
                135,
                197,
                "",
            ),
            (
                "ashby-disabled",
                "Disabled Ashby Co",
                "Backend",
                "ashby",
                "https://disabled.example/job",
                "manual",
                "ashby automation disabled after spam rejection; finish in trusted browser",
                136,
                198,
                "",
            ),
            (
                "needs-answers",
                "Answers Co",
                "Product Engineer",
                "greenhouse",
                "https://answers.example/job",
                "manual",
                "needs answers: sponsorship and start date",
                137,
                199,
                "",
            ),
            (
                "lever-location",
                "Location Co",
                "SWE",
                "watcher-source",
                "https://jobs.lever.co/location/00000000-0000-0000-0000-000000000006",
                "manual",
                "needs manual Lever location selection behind hCaptcha",
                138,
                200,
                "",
            ),
            (
                "email-action",
                "Email Co",
                "Founding Engineer",
                "hn",
                "mailto:jobs@example.com",
                "manual",
                "needs answers: email application details",
                139,
                201,
                "",
            ),
            (
                "waas-action",
                "WaaS Co",
                "Software Engineer",
                "waas",
                "https://www.workatastartup.com/jobs/12345",
                "manual",
                "needs answers: Work at a Startup prompt",
                139,
                202,
                "",
            ),
            (
                "submitted",
                "Submitted Co",
                "SWE",
                "lever",
                "https://submitted.example/job",
                "submitted",
                "Lever hCaptcha requires manual completion",
                140,
                196,
                "",
            ),
        ],
    )
    conn.execute("INSERT INTO emails VALUES (?, ?)", ("captcha", "/Users/david/resume.pdf"))
    conn.executemany(
        """
        INSERT INTO submission_attempts (
            attempt_id, posting_id, ats, lane, worker_id, browser_mode,
            policy_revision, started_at, finished_at, outcome, reason_code,
            raw_reason, click_attempted, confirmation_observed,
            artifact_refs_json, unanswered_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "a1",
                "captcha",
                "lever",
                "lever",
                "w",
                "headless",
                "test",
                170,
                171,
                "manual",
                "manual_captcha",
                "raw answer secret /Users/david/filled.png",
                0,
                0,
                json.dumps({"filled_form_screenshot": "/Users/david/filled.png"}),
                json.dumps([{"question": "raw answer?", "answer": "secret"}]),
            ),
            (
                "a2",
                "uncertain",
                "greenhouse",
                "direct",
                "w",
                "headless",
                "test",
                175,
                176,
                "manual",
                "submission_uncertain",
                "confirmation missing",
                1,
                0,
                "{}",
                "[]",
            ),
            (
                "a3",
                "debt",
                "other",
                "unsupported",
                "w",
                "headless",
                "test",
                192,
                193,
                "manual",
                "manual",
                "no adapter for other",
                0,
                0,
                "{}",
                "[]",
            ),
            (
                "a4",
                "ashby-spam",
                "ashby",
                "ashby",
                "w",
                "headless",
                "test",
                193,
                194,
                "manual",
                "manual",
                "Ashby rejected the submission as possible spam: use normal Chrome",
                0,
                0,
                "{}",
                "[]",
            ),
            (
                "a5",
                "ashby-disabled",
                "ashby",
                "ashby",
                "w",
                "headless",
                "test",
                194,
                195,
                "manual",
                "manual",
                "ashby automation disabled after spam rejection; finish in trusted browser",
                0,
                0,
                "{}",
                "[]",
            ),
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_collect_manual_actions_exposes_safe_fields_only(tmp_path, monkeypatch):
    monkeypatch.setattr(sheet_tracker, "DB", _manual_actions_db(tmp_path))
    conn = sqlite3.connect(sheet_tracker.DB)
    conn.row_factory = sqlite3.Row

    rows = sheet_tracker._collect_manual_actions(conn)
    conn.close()

    assert rows[0] == [
        "Company", "Role", "ATS", "Action", "URL", "Age", "Attempt state",
        "Prepared resume", "Prepared screenshot", "Latest reason",
    ]
    body = rows[1:]
    assert [row[0] for row in body] == [
        "WaaS Co", "Email Co", "Location Co", "Answers Co", "Disabled Ashby Co", "Spam Co",
        "Uncertain Co", "Captcha Co",
    ]
    assert all("Debt Co" not in row and "Submitted Co" not in row for row in body)
    assert any(row[7] is True and row[8] is True for row in body)
    by_company = {row[0]: row for row in body}
    assert by_company["WaaS Co"][2] == "waas"
    assert by_company["Email Co"][2] == "email"
    assert by_company["Location Co"][2] == "lever"
    assert by_company["Location Co"][3] == "Finish manually"
    assert by_company["Answers Co"][3] == "Answer questions"
    assert by_company["Disabled Ashby Co"][3] == "Finish manually"
    assert by_company["Spam Co"][3] == "Finish manually"
    rendered = json.dumps(rows)
    assert "/Users/" not in rendered
    assert "raw answer" not in rendered
    assert "secret" not in rendered


def test_ensure_sheet_adds_manual_actions_once_for_existing_sheet(monkeypatch):
    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"sheets": [{"properties": {"title": "Dashboard", "sheetId": 1}}]}
        return {"replies": [{"addSheet": {"properties": {"sheetId": 99}}}]}

    monkeypatch.setattr(sheet_tracker, "_api", fake_api)

    assert sheet_tracker._ensure_sheet("sid", "Manual Actions") == 99

    assert calls == [
        ("GET", "/sid?fields=sheets.properties", None),
        ("POST", "/sid:batchUpdate", {"requests": [{"addSheet": {"properties": {"title": "Manual Actions", "gridProperties": {"frozenRowCount": 1}}}}]}),
    ]


def test_ensure_sheet_reuses_existing_manual_actions_sheet(monkeypatch):
    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        return {"sheets": [{"properties": {"title": "Manual Actions", "sheetId": 42}}]}

    monkeypatch.setattr(sheet_tracker, "_api", fake_api)

    assert sheet_tracker._ensure_sheet("sid", "Manual Actions") == 42
    assert calls == [("GET", "/sid?fields=sheets.properties", None)]


def test_sync_quotes_manual_actions_a1_ranges(tmp_path, monkeypatch):
    captured = []

    def fake_api(method, path, body=None):
        captured.append((method, path, body))
        if method == "GET":
            return {
                "sheets": [
                    {"properties": {"title": "Dashboard", "sheetId": 1}},
                    {"properties": {"title": "Applications", "sheetId": 2}},
                    {"properties": {"title": "Pipeline", "sheetId": 3}},
                    {"properties": {"title": "Manual Actions", "sheetId": 4}},
                ]
            }
        return {}

    state = tmp_path / "sheet_tracker.json"
    state.write_text(json.dumps({"spreadsheet_id": "sid", "url": "https://sheet.example"}))
    db = tmp_path / "tracker.db"
    db.touch()
    monkeypatch.setattr(sheet_tracker, "STATE", state)
    monkeypatch.setattr(sheet_tracker, "DB", db)
    monkeypatch.setattr(sheet_tracker, "_api", fake_api)
    monkeypatch.setattr(sheet_tracker, "_collect", lambda: ([], [], {"total": 0, "responded": 0, "week": 0, "oa": 0, "interview": 0, "offer": 0, "rejected": 0}))
    monkeypatch.setattr(sheet_tracker, "_collect_manual_actions", lambda conn: [sheet_tracker.MANUAL_ACTION_HEADERS[:]])

    assert sheet_tracker.sync() == "https://sheet.example"

    clear_body = next(body for method, path, body in captured if path == "/sid/values:batchClear")
    update_body = next(body for method, path, body in captured if path == "/sid/values:batchUpdate")
    assert "'Manual Actions'!A:J" in clear_body["ranges"]
    assert any(row["range"] == "'Manual Actions'!A1" for row in update_body["data"])
