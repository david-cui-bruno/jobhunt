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
                [{"faid": "referral", "answer": "No", "label": "Were you referred by a current employee?"}],
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

        source = inspect.getsource(workday.enter_application_form)
        self.assertIn("_workday_outage", source)
        self.assertLess(
            source.index("_workday_outage"),
            source.index("apply button not found"),
        )
        self.assertIn("workday service interruption; retry later", source)
        upload_failure = inspect.getsource(workday.apply_workday)
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

    def test_dropdown_needs_options_for_unapproved_prefill(self) -> None:
        """Amgen 2026-08-17: a saved draft prefilled 'Corporate Website' for
        the recruiting source. The harvest pass must open such dropdowns so
        the grounded pass can render and select the approved replacement."""
        field = {
            "faid": "source|0",
            "label": "How Did You Hear About Us?*",
            "kind": "dropdown",
            "value": "Corporate Website",
        }
        self.assertTrue(workday._dropdown_needs_options(field, "amgen"))
        # Empty dropdowns still harvest; approved prefills do not.
        self.assertTrue(workday._dropdown_needs_options(
            dict(field, value=""), "amgen"))
        self.assertFalse(workday._dropdown_needs_options(
            dict(field, label="Phone Device Type*", value="Mobile"), "amgen"))

    def test_field_of_study_candidates_split_double_major(self) -> None:
        """PSP 2026-08-17: 'Computer Science & Economics' is not a picklist
        leaf; component majors are truthful fallbacks, full text first."""
        field = {"faid": "fos|0", "label": "Field of Study*", "kind": "multiselect"}
        self.assertEqual(
            ["Computer Science & Economics", "Computer Science", "Economics"],
            workday._multiselect_candidates(field, "Computer Science & Economics"),
        )
        unrelated = {"faid": "src|0", "label": "School or University*",
                     "kind": "multiselect"}
        self.assertEqual(
            ["Brown University"],
            workday._multiselect_candidates(unrelated, "Brown University"),
        )


APPLY_URL = "https://example.wd1.myworkdayjobs.com/jobs/job/example"


class _EntryLocator:
    def __init__(self, page, selector: str, count: int, visible: bool = True):
        self.page = page
        self.selector = selector
        self.created_generation = page.generation
        self._count = count
        self._visible = visible
        self.first = self
        self.last = self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible and self._count > 0

    def click(self, **kwargs):
        if self.created_generation != self.page.generation:
            self.page.stale_clicks.append(self.selector)
        if "adventureButton" in self.selector:
            self.page.apply_clicks += 1
        if "autofillWithResume" in self.selector:
            self.page.autofill_clicks += 1

    def wait_for(self, **kwargs):
        if not self.count():
            raise TimeoutError(self.selector)

    def set_input_files(self, value):
        self.page.uploaded_files.append(value)

    def inner_text(self):
        return self.page.body_text


class RecoveryThenApplicationPage:
    def __init__(self):
        self.goto_calls = []
        self.generation = 0
        self.apply_clicks = 0
        self.autofill_clicks = 0
        self.stale_clicks = []
        self.uploaded_files = []
        self.frames = [self]
        self.url = "about:blank"

    @property
    def body_text(self):
        if len(self.goto_calls) == 1:
            return "Sign In Create Account"
        return "Software Engineer Apply Autofill with Resume"

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.generation += 1
        self.url = url

    def wait_for_timeout(self, _ms):
        return None

    def inner_text(self, selector):
        assert selector == "body"
        return self.body_text

    def locator(self, selector):
        auth_stage = len(self.goto_calls) == 1
        app_stage = len(self.goto_calls) >= 2
        if selector == "body":
            return _EntryLocator(self, selector, 1, True)
        if "adventureButton" in selector:
            return _EntryLocator(self, selector, 1 if app_stage else 0, app_stage)
        if "legalNoticeAcceptButton" in selector or "onetrust" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "autofillWithResume" in selector:
            return _EntryLocator(self, selector, 1 if app_stage else 0, app_stage)
        if "file-upload-input-ref" in selector:
            return _EntryLocator(self, selector, 1 if app_stage else 0, app_stage)
        if "SignInWithEmailButton" in selector:
            return _EntryLocator(self, selector, 1 if auth_stage else 0, auth_stage)
        if "createAccountSubmitButton" in selector or "signInSubmitButton" in selector:
            return _EntryLocator(self, selector, 1 if auth_stage else 0, auth_stage)
        if "progressBarActiveStep" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "pageFooterNextButton" in selector or "formField-" in selector:
            return _EntryLocator(self, selector, 0, False)
        return _EntryLocator(self, selector, 0, False)


class PostApplyAuthGatePage(RecoveryThenApplicationPage):
    @property
    def body_text(self):
        return "Software Engineer Apply Autofill with Resume Sign In Create Account"

    def locator(self, selector):
        if "adventureButton" in selector:
            return _EntryLocator(self, selector, 1, True)
        if "autofillWithResume" in selector:
            return _EntryLocator(self, selector, 1, True)
        if "file-upload-input-ref" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "SignInWithEmailButton" in selector:
            return _EntryLocator(self, selector, 1, True)
        if "createAccountSubmitButton" in selector or "signInSubmitButton" in selector:
            return _EntryLocator(self, selector, 1, True)
        return super().locator(selector)


class MissingApplyPage:
    def __init__(
        self,
        body_text: str,
        explicit_href: str | None = None,
        *,
        broad_apply_href: str | None = None,
    ):
        self._body_text = body_text
        self.explicit_href = explicit_href
        self.broad_apply_href = broad_apply_href
        self.goto_calls = []
        self.generation = 0
        self.apply_clicks = 0
        self.autofill_clicks = 0
        self.stale_clicks = []
        self.uploaded_files = []
        self.frames = [self]
        self.url = "about:blank"

    @property
    def body_text(self):
        return self._body_text

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.generation += 1
        self.url = url
        if self.explicit_href and len(self.goto_calls) >= 2:
            self._body_text = "Application Autofill with Resume"

    def wait_for_timeout(self, _ms):
        return None

    def inner_text(self, selector):
        assert selector == "body"
        return self.body_text

    def locator(self, selector):
        if selector == "body":
            return _EntryLocator(self, selector, 1, True)
        if "href" in selector and "Apply" in selector:
            return _HrefLocator(self, selector, self.explicit_href or self.broad_apply_href)
        if "adventureButton" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "legalNoticeAcceptButton" in selector or "onetrust" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "autofillWithResume" in selector:
            ready = len(self.goto_calls) >= 2
            return _EntryLocator(self, selector, 1 if ready else 0, ready)
        if "file-upload-input-ref" in selector:
            ready = len(self.goto_calls) >= 2
            return _EntryLocator(self, selector, 1 if ready else 0, ready)
        if "SignInWithEmailButton" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "createAccountSubmitButton" in selector or "signInSubmitButton" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "progressBarActiveStep" in selector:
            return _EntryLocator(self, selector, 0, False)
        if "pageFooterNextButton" in selector or "formField-" in selector:
            return _EntryLocator(self, selector, 0, False)
        return _EntryLocator(self, selector, 0, False)

    def evaluate(self, script):
        if self.explicit_href:
            return self.explicit_href
        return None


class _ContinueApplicationControl(_EntryLocator):
    def click(self, **kwargs):
        self.page.continued = True
        self.page.apply_clicks += 1


class _ContinueApplicationMatches:
    def __init__(self, page, count: int):
        self.page = page
        self._count = count

    def count(self):
        return self._count

    def nth(self, index: int):
        return _ContinueApplicationControl(
            self.page,
            "button[accessible-name='Continue Application']",
            1 if index < self._count else 0,
            True,
        )


class ContinueApplicationPage(MissingApplyPage):
    def __init__(self, *, accessible_matches: int = 1):
        super().__init__("Software Engineer Intern Continue Application")
        self.accessible_matches = accessible_matches
        self.continued = False

    def get_by_role(self, role, *, name, exact):
        assert role == "button"
        assert name == "Continue Application"
        assert exact is True
        return _ContinueApplicationMatches(self, self.accessible_matches)

    def locator(self, selector):
        if "autofillWithResume" in selector or "file-upload-input-ref" in selector:
            return _EntryLocator(self, selector, 1 if self.continued else 0, self.continued)
        return super().locator(selector)


class _HrefLocator(_EntryLocator):
    def __init__(self, page, selector: str, href: str | None):
        super().__init__(page, selector, 1 if href else 0, bool(href))
        self.href = href

    def get_attribute(self, name: str):
        if name == "href":
            return self.href
        return None


def entry_result_for_body(body_text: str) -> workday.WorkdayEntryResult:
    return workday.enter_application_form(MissingApplyPage(body_text), APPLY_URL)


def test_missing_apply_with_closed_marker_is_stale():
    result = entry_result_for_body("This job is no longer available")
    assert result == workday.WorkdayEntryResult("closed", "posting closed", "job is no longer available")


def test_missing_apply_with_real_unavailable_posting_phrase_is_stale():
    result = entry_result_for_body("This job posting is no longer available")
    assert result == workday.WorkdayEntryResult(
        "closed", "posting closed", "posting is no longer available"
    )


def test_missing_apply_without_closed_marker_is_retryable():
    result = entry_result_for_body("Welcome to careers")
    assert result.state == "retryable"
    assert result.reason == "apply button not found"


def test_missing_apply_uses_one_explicit_application_href_without_looping():
    page = MissingApplyPage("Welcome to careers Apply", explicit_href="/jobs/job/example/apply")

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult(
        "upload_ready", "used explicit application href fallback", "explicit_application_href"
    )
    assert page.goto_calls == [
        APPLY_URL,
        "https://example.wd1.myworkdayjobs.com/jobs/job/example/apply",
    ]


def test_missing_apply_uses_one_exact_visible_continue_application_button():
    page = ContinueApplicationPage()

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult("upload_ready")
    assert page.apply_clicks == 1
    assert page.goto_calls == [APPLY_URL]


def test_missing_apply_rejects_ambiguous_continue_application_buttons():
    page = ContinueApplicationPage(accessible_matches=2)

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult("retryable", "apply button not found")
    assert page.apply_clicks == 0
    assert page.goto_calls == [APPLY_URL]


def test_missing_apply_ignores_apply_filters_text_fallback_and_remains_retryable():
    page = MissingApplyPage(
        "Welcome to careers Apply filters",
        broad_apply_href="/jobs/job/example/apply?filter=true",
    )

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult("retryable", "apply button not found")
    assert page.goto_calls == [APPLY_URL]


def test_missing_apply_rejects_cross_host_application_href_and_remains_retryable():
    page = MissingApplyPage(
        "Welcome to careers Start Your Application",
        explicit_href="https://other.wd1.myworkdayjobs.com/jobs/job/example/apply",
    )

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult("retryable", "apply button not found")
    assert page.goto_calls == [APPLY_URL]


def test_missing_apply_rejects_cross_posting_application_href_and_remains_retryable():
    page = MissingApplyPage(
        "Welcome to careers Start Your Application",
        explicit_href="/jobs/job/different/apply",
    )

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult("retryable", "apply button not found")
    assert page.goto_calls == [APPLY_URL]


def test_missing_apply_accepts_location_segmented_same_requisition_with_apply():
    apply_url = "https://example.wd1.myworkdayjobs.com/en-US/careers/job/NYC/Role_R123"
    page = MissingApplyPage(
        "Welcome to careers Start Your Application",
        explicit_href="/en-US/careers/job/NYC/Role_R123/apply",
    )

    result = workday.enter_application_form(page, apply_url)

    assert result == workday.WorkdayEntryResult(
        "upload_ready", "used explicit application href fallback", "explicit_application_href"
    )
    assert page.goto_calls == [
        apply_url,
        "https://example.wd1.myworkdayjobs.com/en-US/careers/job/NYC/Role_R123/apply",
    ]


def test_missing_apply_rejects_location_segmented_different_requisition():
    apply_url = "https://example.wd1.myworkdayjobs.com/en-US/careers/job/NYC/Role_R123"
    page = MissingApplyPage(
        "Welcome to careers Start Your Application",
        explicit_href="/en-US/careers/job/NYC/Other_R999/apply",
    )

    result = workday.enter_application_form(page, apply_url)

    assert result == workday.WorkdayEntryResult("retryable", "apply button not found")
    assert page.goto_calls == [apply_url]


def test_missing_apply_rejects_https_to_http_application_href():
    page = MissingApplyPage(
        "Welcome to careers Start Your Application",
        explicit_href="http://example.wd1.myworkdayjobs.com/jobs/job/example/apply",
    )

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult("retryable", "apply button not found")
    assert page.goto_calls == [APPLY_URL]


def test_workday_closed_entry_maps_to_stale_adapter_result(monkeypatch, tmp_path):
    page = mock.Mock()
    monkeypatch.setattr(workday, "sync_playwright", lambda: _FakePlaywrightContext(page))
    monkeypatch.setattr(workday, "configure_page", lambda page: page)
    monkeypatch.setattr(
        workday,
        "enter_application_form",
        lambda page, apply_url: workday.WorkdayEntryResult(
            "closed", "posting closed", "job is no longer available"
        ),
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")

    result = workday.apply_workday(APPLY_URL, resume, "slug", dry_run=True)

    assert result["outcome"] == "stale"
    assert result["retryable"] is False
    assert result["click_attempted"] is False
    assert result["reason"] == "posting closed: job is no longer available"


def test_workday_retryable_entry_maps_to_retryable_adapter_result(monkeypatch, tmp_path):
    page = mock.Mock()
    monkeypatch.setattr(workday, "sync_playwright", lambda: _FakePlaywrightContext(page))
    monkeypatch.setattr(workday, "configure_page", lambda page: page)
    monkeypatch.setattr(
        workday,
        "enter_application_form",
        lambda page, apply_url: workday.WorkdayEntryResult("retryable", "apply button not found"),
    )
    monkeypatch.setattr(workday, "_shot", lambda *args, **kwargs: None)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")

    result = workday.apply_workday(APPLY_URL, resume, "slug", dry_run=True)

    assert result["outcome"] == "retryable_failure"
    assert result["retryable"] is True
    assert result["click_attempted"] is False
    assert result["reason"] == "apply button not found"


def test_post_apply_auth_gate_delegates_to_outer_reentry(monkeypatch):
    page = PostApplyAuthGatePage()
    create_account = mock.Mock()
    sign_in = mock.Mock()
    monkeypatch.setattr(workday, "maybe_create_account", create_account)
    monkeypatch.setattr(workday, "maybe_sign_in", sign_in)

    result = workday.enter_application_form(page, APPLY_URL)

    assert result.state == "auth_required"
    assert page.apply_clicks == 1
    assert page.autofill_clicks == 1
    create_account.assert_not_called()
    sign_in.assert_not_called()


def test_workday_already_applied_entry_maps_to_manual_safe_result(monkeypatch, tmp_path):
    page = mock.Mock()
    monkeypatch.setattr(workday, "sync_playwright", lambda: _FakePlaywrightContext(page))
    monkeypatch.setattr(workday, "configure_page", lambda page: page)
    monkeypatch.setattr(
        workday,
        "enter_application_form",
        lambda page, apply_url: workday.WorkdayEntryResult(
            "already_applied",
            "Workday reports already applied; needs verification before any further action",
        ),
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")

    result = workday.apply_workday(APPLY_URL, resume, "slug", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["retryable"] is False
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert result["submitted"] is False
    assert result["reason"] == (
        "Workday reports already applied; needs verification before any further action"
    )


def test_already_applied_evidence_stops_before_apply_click():
    page = MissingApplyPage("You've already applied for this job. Apply")

    result = workday.enter_application_form(page, APPLY_URL)

    assert result == workday.WorkdayEntryResult(
        "already_applied",
        "Workday reports already applied; needs verification before any further action",
    )
    assert page.apply_clicks == 0


def test_already_applied_evidence_matches_curly_apostrophe_ocr():
    assert workday._workday_already_applied("You’ve already applied for this job.")


def test_successful_recovery_reenters_apply_and_autofill():
    page = RecoveryThenApplicationPage()
    first = workday.enter_application_form(page, APPLY_URL)
    assert first.state == "auth_required"

    result = workday.enter_application_form(page, APPLY_URL)

    assert page.goto_calls == [APPLY_URL, APPLY_URL]
    assert page.apply_clicks == 1
    assert page.autofill_clicks == 1
    assert result.state == "upload_ready"


def test_reentry_uses_only_fresh_locators_after_navigation():
    page = RecoveryThenApplicationPage()
    workday.enter_application_form(page, APPLY_URL)

    result = workday.enter_application_form(page, APPLY_URL)

    assert result.state == "upload_ready"
    assert page.stale_clicks == []


def test_saved_draft_entry_bypasses_initial_upload_and_refreshes_resume(monkeypatch, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")
    page = mock.Mock()
    entry = workday.WorkdayEntryResult(state="upload_ready", marker="saved_draft")
    monkeypatch.setattr(workday, "enter_application_form", lambda page, apply_url: entry)
    monkeypatch.setattr(workday, "saved_draft_wizard_is_active", lambda page: True)
    monkeypatch.setattr(workday, "current_step", lambda page: "My Experience")
    refresh = mock.Mock(return_value=True)
    monkeypatch.setattr(workday, "refresh_saved_resume", refresh)

    resume_current = workday.prepare_workday_resume_entry(page, resume, entry)

    assert resume_current is True
    refresh.assert_called_once_with(page, resume)


def test_saved_draft_my_information_defers_resume_refresh(monkeypatch, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")
    page = mock.Mock()
    entry = workday.WorkdayEntryResult(state="upload_ready", marker="saved_draft")
    monkeypatch.setattr(workday, "saved_draft_wizard_is_active", lambda page: True)
    monkeypatch.setattr(workday, "current_step", lambda page: "My Information")
    refresh = mock.Mock(return_value=False)
    monkeypatch.setattr(workday, "refresh_saved_resume", refresh)

    resume_current = workday.prepare_workday_resume_entry(page, resume, entry)

    assert resume_current is None
    refresh.assert_not_called()


class _FakePlaywrightContext:
    def __init__(self, page):
        self.page = page
        self.chromium = self
        self.browser = self
        self.context = self
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def launch(self, **_kwargs):
        return self.browser

    def new_context(self, **_kwargs):
        return self.context

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


def _apply_with_second_entry_gate(monkeypatch, tmp_path, verification_required):
    page = mock.Mock()
    entries = [
        workday.WorkdayEntryResult("auth_required", "workday account access required"),
        workday.WorkdayEntryResult("auth_required", "still blocked detail"),
    ]
    monkeypatch.setattr(workday, "sync_playwright", lambda: _FakePlaywrightContext(page))
    monkeypatch.setattr(workday, "configure_page", lambda page: page)
    monkeypatch.setattr(workday, "enter_application_form", lambda page, apply_url: entries.pop(0))
    monkeypatch.setattr(workday, "ensure_workday_account_access", lambda *args: (True, ""))
    monkeypatch.setattr(workday, "_verification_required", lambda page: verification_required)
    monkeypatch.setattr(workday, "_shot", lambda *args, **kwargs: None)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")

    return workday.apply_workday(APPLY_URL, resume, "slug", dry_run=True)


def test_second_entry_auth_required_labels_verification_gate(monkeypatch, tmp_path):
    result = _apply_with_second_entry_gate(monkeypatch, tmp_path, verification_required=True)

    assert result["ok"] is True
    assert result["reason"] == "still blocked detail"
    assert result["unanswered"] == ["Workday account verification"]


def test_second_entry_auth_required_labels_sign_in_gate(monkeypatch, tmp_path):
    result = _apply_with_second_entry_gate(monkeypatch, tmp_path, verification_required=False)

    assert result["ok"] is True
    assert result["reason"] == "still blocked detail"
    assert result["unanswered"] == ["Workday account sign-in"]


if __name__ == "__main__":
    unittest.main()
