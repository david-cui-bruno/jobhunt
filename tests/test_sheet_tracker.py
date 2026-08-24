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
            ats TEXT,
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
                "lever",
                "https://location.example/job",
                "manual",
                "needs manual Lever location selection behind hCaptcha",
                138,
                200,
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
        "Location Co", "Answers Co", "Disabled Ashby Co", "Spam Co",
        "Uncertain Co", "Captcha Co",
    ]
    assert all("Debt Co" not in row and "Submitted Co" not in row for row in body)
    assert any(row[7] is True and row[8] is True for row in body)
    by_company = {row[0]: row for row in body}
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
