from __future__ import annotations

import base64
import io
import inspect
import json
import sys
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "apply"))

import workday


class WorkdayAnswerTests(unittest.TestCase):
    def test_company_website_source_navigates_hierarchy_without_other_fallback(self):
        self.assertEqual(
            "Valeo Websites",
            workday._workday_prompt_target(
                "Company website",
                ["Employee Referral", "Jobboards", "Valeo Websites"],
                "valeo",
            ),
        )
        self.assertEqual(
            "Valeo Website",
            workday._workday_prompt_target(
                "Company website",
                ["Other (Website)", "Valeo.hu", "Valeo Website"],
                "valeo",
            ),
        )
        self.assertIsNone(
            workday._workday_prompt_target(
                "Company website",
                ["Other (Website)", "Careers Website", "Corporate Website"],
                "valeo",
            )
        )

    def test_company_website_leaf_is_safe_but_other_prefill_is_not(self):
        approved = {
            "company_facts": {
                "Valeo": {"recruiting_source": "Company website"},
            },
        }
        field = {
            "faid": "source|0",
            "label": "How Did You Hear About Us?*",
            "kind": "multiselect",
            "value": "Valeo Website",
        }
        self.assertEqual(
            [],
            workday.unsafe_prefilled_fields(
                [field], "valeo", approved_answers=approved
            ),
        )
        for unsafe_value in (
            "Other (Website)", "Employee Referral", "LinkedIn Website"
        ):
            with self.subTest(value=unsafe_value):
                self.assertEqual(
                    ["How Did You Hear About Us?*"],
                    workday.unsafe_prefilled_fields(
                        [dict(field, value=unsafe_value)],
                        "valeo",
                        approved_answers=approved,
                    ),
                )

    def test_activation_link_must_match_the_exact_workday_tenant(self) -> None:
        good = "https://nelnet.wd1.myworkdayjobs.com/MyNelnet/activate/secret-token"
        evil = "https://evil.example/activate/stolen"
        html_body = f'<a href="{evil}">bad</a><a href="{good}">verify</a>'
        encoded = base64.urlsafe_b64encode(html_body.encode()).decode().rstrip("=")
        message = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/html", "body": {"data": encoded}}],
            }
        }

        self.assertEqual(
            good,
            workday.workday_activation_url_from_message(
                message, "nelnet.wd1.myworkdayjobs.com"
            ),
        )
        self.assertIsNone(
            workday.workday_activation_url_from_message(
                message, "psu.wd1.myworkdayjobs.com"
            )
        )

    def test_activation_lookup_supports_branded_sender_domains(self) -> None:
        good = "https://valeo.wd3.myworkdayjobs.com/valeo_jobs/activate/token"
        encoded = base64.urlsafe_b64encode(
            f'<a href="{good}">verify</a>'.encode()
        ).decode().rstrip("=")
        message = {
            "internalDate": "2000000000000",
            "payload": {
                "mimeType": "text/html",
                "body": {"data": encoded},
                "headers": [
                    {"name": "From", "value": "Workday - Valeo <workday@valeo.com>"}
                ],
            },
        }
        calls: list[str] = []

        def fake_call(path: str) -> dict:
            calls.append(path)
            if path.startswith("/messages?q="):
                return {"messages": [{"id": "branded-sender"}]}
            return message

        fake_mailer = types.SimpleNamespace(_call=fake_call)
        with mock.patch.dict(sys.modules, {"mailer": fake_mailer}):
            self.assertEqual(
                good,
                workday.fetch_workday_activation_url(
                    "valeo", "valeo.wd3.myworkdayjobs.com", not_before=0
                ),
            )

        query = urllib.parse.unquote(calls[0])
        self.assertIn('subject:"Verify your candidate account"', query)
        self.assertNotIn("from:", query)

    def test_new_workday_social_auth_chooser_is_supported(self) -> None:
        source = inspect.getsource(workday._open_email_auth)
        self.assertIn("utilityButtonSignIn", source)
        self.assertIn("SignInWithEmailButton", source)
        self.assertIn("createAccountLink", source)
        self.assertIn("_visible_locator_in_frames", source)
        self.assertLess(
            source.index("SignInWithEmailButton"),
            source.index("utilityButtonSignIn"),
        )
        self.assertNotIn("_account_scope", source)
        self.assertIn("page.frames", inspect.getsource(workday._visible_locator_in_frames))

    def test_localized_workday_click_filters_are_supported(self) -> None:
        self.assertIn("CreateAccount", workday.CREATE_ACCOUNT_OVERLAY)
        self.assertIn("SignIn", workday.SIGN_IN_OVERLAY)

    def test_generic_submit_filter_is_scoped_to_the_exact_auth_button(self) -> None:
        scope = mock.Mock()
        button_lookup = mock.Mock()
        button = mock.Mock()
        wrapper = mock.Mock()
        overlay_lookup = mock.Mock()
        overlay = mock.Mock()
        scope.locator.return_value = button_lookup
        button_lookup.last = button
        button.locator.return_value = wrapper
        wrapper.locator.return_value = overlay_lookup
        overlay_lookup.first = overlay
        overlay.count.return_value = 1

        workday._click_workday_submit(scope, "signInSubmitButton")

        scope.locator.assert_called_once_with(
            '[data-automation-id="signInSubmitButton"]'
        )
        button.locator.assert_called_once_with("xpath=..")
        wrapper.locator.assert_called_once_with(
            ":scope > [data-automation-id='click_filter']"
        )
        overlay.click.assert_called_once_with(timeout=5000)
        button.click.assert_not_called()

    def test_visible_workday_auth_error_is_reported_and_recoverable(self) -> None:
        page = mock.Mock()
        frame = mock.Mock()
        lookup = mock.Mock()
        error = mock.Mock()
        page.frames = [frame]
        frame.locator.return_value = lookup
        lookup.last = error
        error.count.return_value = 1
        error.is_visible.return_value = True
        error.inner_text.return_value = (
            "You may have entered the wrong email address or password "
            "or your account might be locked."
        )

        self.assertIn("wrong email address", workday._workday_auth_error(page))
        source = inspect.getsource(workday.ensure_workday_account_access)
        self.assertIn("createAccountLink", source)
        self.assertIn("_workday_auth_gate_visible", source)
        self.assertIn(
            "maybe_create_account(page, company_key)\n"
            "        _open_email_auth(page, create_account=False)\n"
            "        maybe_sign_in(page, company_key)",
            source,
        )

    def test_prompt_includes_grounding_without_missing_stories_placeholder(self) -> None:
        response = io.StringIO(json.dumps({"content": [{"type": "text", "text": "[]"}]}))
        fields = [
            {
                "faid": "question-1",
                "value": "",
                "label": "Why are you interested?",
                "kind": "text",
                "options": [],
            }
        ]

        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(workday.wd_answers(fields, "Example", "Engineer"), [])

    def test_saved_draft_answers_are_checked_against_approved_facts(self) -> None:
        approved = {
            "education": {
                "expected_graduation_month": "June",
                "expected_graduation_year": "2028",
                "exact_graduation_date": None,
            },
            "company_facts": {
                "LPL Financial": {"referral": False},
            },
        }
        fields = [
            {
                "faid": "referral-right",
                "label": "Were you referred by a current employee?",
                "value": "No",
            },
            {
                "faid": "referral-wrong",
                "label": "Were you referred by a current employee?",
                "value": "Yes",
            },
            {
                "faid": "grad-day",
                "label": "Graduation date",
                "kind": "date",
                "hasDay": True,
                "value": "05/15/2028",
            },
        ]

        self.assertEqual(
            ["Were you referred by a current employee?", "Graduation date"],
            workday.unsafe_prefilled_fields(
                fields,
                "lplfinancial",
                approved_answers=approved,
            ),
        )

    def test_existing_workday_wizard_can_resume_without_initial_upload_zone(self) -> None:
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        with mock.patch.object(workday, "current_step", return_value="My Information"):
            self.assertTrue(workday.saved_draft_wizard_is_active(page))
        with mock.patch.object(workday, "current_step", return_value=""):
            self.assertFalse(workday.saved_draft_wizard_is_active(page))

    def test_saved_resume_refresh_replaces_stale_attachments_safely(self) -> None:
        """Cadence 2026-08-17: a resumed draft held a stale differently-named
        PDF, so the old same-name-only refresh failed closed and the posting
        went manual. The refresh must clear the draft's own attachments (all
        uploaded by this automation), upload the current PDF, and verify only
        that exact file remains; unexpected control shapes still fail closed."""
        source = inspect.getsource(workday.refresh_saved_resume)
        self.assertIn('expected_label = f"Delete {resume_pdf.name}"', source)
        # Verification that exactly the current file remains after upload.
        self.assertIn("replacement.count() == 1", source)
        # Only Workday's delete-file controls may be touched; unknown labels bail.
        self.assertIn('label.startswith("Delete ")', source)
        self.assertIn("if deletes.count():", source)

    def test_model_outage_returns_no_guesses(self) -> None:
        fields = [
            {
                "faid": "question-1",
                "value": "",
                "label": "Why are you interested?",
                "kind": "text",
                "options": [],
            }
        ]
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
            self.assertEqual(workday.wd_answers(fields, "Example", "Engineer"), [])

    def test_approved_workday_fact_does_not_require_the_model(self) -> None:
        fields = [
            {
                "faid": "referral",
                "value": "",
                "label": "Were you referred by a current employee?",
                "kind": "radio",
                "options": ["Yes", "No"],
            }
        ]
        approved = {"company_facts": {"LPL Financial": {"referral": False}}}
        with mock.patch.object(workday.qa, "APPLICATION_ANSWERS", approved), \
             mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(
                workday.wd_answers(fields, "lplfinancial", "Software Engineer Intern"),
                [{"faid": "referral", "answer": "No"}],
            )
        urlopen.assert_not_called()

    def test_activation_search_reaches_back_to_account_creation(self) -> None:
        """A 48h Gmail clamp orphaned every account older than two days.

        Workday sends the activation email exactly once, at account creation.
        Retried postings whose account was created earlier (nelnet, haier,
        cadence, ... all 2.7+ days old on 2026-08-16) could never find it and
        settled as "resume upload zone never appeared".
        """
        created_at = 1_700_000_000  # far older than any 48h window
        calls: list[str] = []

        def fake_call(path: str) -> dict:
            calls.append(path)
            return {"messages": []}

        fake_mailer = types.SimpleNamespace(_call=fake_call)
        with mock.patch.dict(sys.modules, {"mailer": fake_mailer}), \
                mock.patch.object(workday.time, "sleep"):
            workday.fetch_workday_activation_url(
                "nelnet", "nelnet.wd1.myworkdayjobs.com", not_before=created_at
            )

        query = urllib.parse.unquote(calls[0])
        self.assertIn(f"after:{created_at - 300}", query)

    def test_upload_zone_failure_reports_visible_auth_gates_honestly(self) -> None:
        """First American 2026-08-15: the run failed as "resume upload zone
        never appeared" while its screenshot showed the verification sign-in
        form. The mislabel hid the recoverable cause and settled the posting.
        """
        source = inspect.getsource(workday.apply_workday)
        upload_failure = source[source.index("resume_current = False"):]
        self.assertIn("_verification_required(page)", upload_failure)
        self.assertIn("_workday_auth_gate_visible(page)", upload_failure)
        self.assertLess(
            upload_failure.index("_verification_required(page)"),
            upload_failure.index("resume upload zone never appeared"),
        )
        self.assertLess(
            upload_failure.index("_workday_auth_gate_visible(page)"),
            upload_failure.index("resume upload zone never appeared"),
        )

    def test_missing_activation_email_attempts_resend_before_giving_up(self) -> None:
        source = inspect.getsource(workday.ensure_workday_account_access)
        self.assertIn("resend", source.lower())
        self.assertLess(
            source.index("Resend"),
            source.index("workday account verification email not found"),
        )

    def test_duplicated_auth_inputs_fill_without_strict_mode_violation(self) -> None:
        """Nelnet 2026-08-16: the Create Account dialog held two identical
        ``email`` inputs, so an unscoped fill crashed Playwright strict mode.
        Fills must target the submit button's own form/dialog and pick one
        element deterministically.
        """
        for fn in (workday.maybe_create_account, workday.maybe_sign_in):
            source = inspect.getsource(fn)
            self.assertIn("_auth_fields_scope", source)
            self.assertIn("_fill_auth_field", source)
            self.assertNotIn(
                "scope.locator(\"input[data-automation-id='email']\").fill", source
            )

        container = mock.Mock()
        field_lookup = mock.Mock()
        field = mock.Mock()
        container.locator.return_value = field_lookup
        field_lookup.count.return_value = 2  # duplicated input
        field_lookup.last = field

        workday._fill_auth_field(
            container, "input[data-automation-id='email']", "me@example.com"
        )
        field.fill.assert_called_once_with("me@example.com")

        scope_source = inspect.getsource(workday._auth_fields_scope)
        self.assertIn("ancestor::*[self::form or @role='dialog'][1]", scope_source)

    def test_workday_outage_is_retryable_not_a_hard_failure(self) -> None:
        """Cadence 2026-08-17: a tenant-wide 'Workday is currently unavailable'
        interruption page settled as a permanent posting failure."""
        page = mock.Mock()
        page.inner_text.return_value = (
            "Workday is currently unavailable.\n"
            "We are experiencing a service interruption."
        )
        self.assertTrue(workday._workday_outage(page))

        page.inner_text.return_value = "Intern Program - Agentic AI Create Account"
        self.assertFalse(workday._workday_outage(page))

        source = inspect.getsource(workday.apply_workday)
        self.assertIn("_workday_outage", source)
        self.assertLess(
            source.index("_workday_outage"),
            source.index("apply button not found"),
        )
        self.assertIn("workday service interruption; retry later", source)
        upload_failure = source[source.index("resume_current = False"):]
        self.assertLess(
            upload_failure.index("_workday_outage(page)"),
            upload_failure.index("resume upload zone never appeared"),
        )

    def test_searchable_source_prompt_expands_approved_candidates(self) -> None:
        """Cadence 2026-08-17: the recruiting-source prompt lists concrete
        leaves (LinkedIn, Indeed, Glassdoor.com), never the generic approved
        intent 'Social media'. The filler must fall back to the other
        user-approved recruiting sources instead of failing forever."""
        approved = {
            "preferences": {
                "recruiting_sources": [
                    "Social media", "Google", "LinkedIn", "Indeed",
                ],
            },
        }
        field = {
            "faid": "source|0",
            "label": "How Did You Hear About Us?*",
            "kind": "multiselect",
            "company_context": "cadence",
        }
        with mock.patch.object(workday.qa, "APPLICATION_ANSWERS", approved):
            candidates = workday._multiselect_candidates(field, "Social media")
        self.assertEqual(
            ["Social media", "Google", "LinkedIn", "Indeed"], candidates
        )
        # Non-recruiting multiselects must not inherit recruiting sources.
        other = dict(field, label="Preferred Office Locations")
        with mock.patch.object(workday.qa, "APPLICATION_ANSWERS", approved):
            self.assertEqual(
                ["Boston"], workday._multiselect_candidates(other, "Boston")
            )

    def test_multiselect_search_uses_key_events_and_exact_unique_match(self) -> None:
        """Cadence 2026-08-17: fill() set the input value without key events,
        so the searchable prompt never filtered and the fill returned False on
        every pass. The search must type real keystrokes and only click an
        exact, unique, popup-scoped option."""
        source = inspect.getsource(workday._multiselect_search_pick)
        self.assertIn("press_sequentially", source)
        fill_source = inspect.getsource(workday.wd_fill)
        self.assertIn("_multiselect_search_pick", fill_source)
        self.assertIn("_multiselect_candidates", fill_source)
        self.assertNotIn("inp.fill(str(answer)[:50])", fill_source)

        prompt_source = inspect.getsource(workday._visible_prompt_options)
        self.assertIn("wd-popup", prompt_source)

    def test_selected_leaf_counts_as_approved_recruiting_source(self) -> None:
        """A selected concrete leaf (LinkedIn) must satisfy both the rendered-
        answer check and the draft-safety check for the generic intent."""
        approved = {
            "preferences": {
                "recruiting_sources": ["Social media", "LinkedIn"],
            },
        }
        field = {
            "faid": "source|0",
            "label": "How Did You Hear About Us?*",
            "kind": "multiselect",
            "value": "LinkedIn",
        }
        self.assertTrue(
            workday._workday_rendered_answer_matches(
                field, "Social media", "LinkedIn", "cadence",
                approved_answers=approved,
            )
        )
        self.assertFalse(
            workday._workday_rendered_answer_matches(
                field, "Social media", "Employee Referral", "cadence",
                approved_answers=approved,
            )
        )
        self.assertEqual(
            [],
            workday.unsafe_prefilled_fields(
                [field], "cadence", approved_answers=approved
            ),
        )

    def test_screenshot_stage_names_cannot_embed_newlines(self) -> None:
        """Core & Main 2026-08-17: current_step() text is multi-line, which
        produced screenshot filenames containing a raw newline."""
        page = mock.Mock()
        with mock.patch.object(workday, "SHOTS", Path("/tmp/shots-test")):
            workday._shot(page, "slug", "current step 2 of 7\nMy Information")
        path = page.screenshot.call_args.kwargs["path"]
        self.assertNotIn("\n", path)
        self.assertTrue(path.endswith(".png"))


if __name__ == "__main__":
    unittest.main()
