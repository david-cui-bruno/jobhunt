import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))
import qa  # noqa: E402
import oraclecloud  # noqa: E402


def _extract_from_html(html):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            return page.evaluate(qa.EXTRACT_JS)
        finally:
            browser.close()


def _required_empty_from_html(html):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            return page.evaluate(oraclecloud.REQUIRED_EMPTY_JS)
        finally:
            browser.close()


def test_extract_js_is_browser_parseable():
    assert _extract_from_html("<form></form>") == []


def test_nameless_yes_no_radio_pair_uses_outer_question_label_once():
    controls = _extract_from_html(
        """
        <div role="radiogroup" aria-labelledby="q-outside-us">
          <div id="q-outside-us">Are you authorized to work in the United States?</div>
          <div class="oj-flex"><label><input type="radio" required> Yes</label></div>
          <div class="oj-flex"><label><input type="radio" required> No</label></div>
        </div>
        """
    )

    assert controls == [{
        "id": "", "name": "", "tag": "input", "type": "group-radio", "cls": "",
        "chosen": "", "label": "Are you authorized to work in the United States?",
        "required": True, "value": "", "options": ["Yes", "No"],
    }]


def test_inner_oj_flex_option_wrappers_group_by_outer_radiogroup_not_each_option():
    controls = _extract_from_html(
        """
        <section role="radiogroup" aria-labelledby="work-auth-question">
          <h2 id="work-auth-question">Will you now or in the future require sponsorship?</h2>
          <div class="oj-flex"><label><input type="radio"> Yes</label></div>
          <div class="oj-flex"><label><input type="radio"> No</label></div>
        </section>
        """
    )

    assert len(controls) == 1
    assert controls[0]["label"] == "Will you now or in the future require sponsorship?"
    assert controls[0]["options"] == ["Yes", "No"]


def test_hidden_named_oracle_radio_inputs_use_visible_radiogroup():
    controls = _extract_from_html(
        """
        <div class="input-row input-row--radiogroup" role="radiogroup" aria-labelledby="age-label">
          <div id="age-label">Are you 18 years old or over?</div>
          <label for="age-yes">Yes</label>
          <input id="age-yes" class="input-row__hidden-control" type="radio"
                 name="age-question" required style="display:none" value="yes">
          <label for="age-no">No</label>
          <input id="age-no" class="input-row__hidden-control" type="radio"
                 name="age-question" required style="display:none" value="no">
        </div>
        """
    )

    assert controls == [{
        "id": "", "name": "age-question", "tag": "input", "type": "group-radio",
        "cls": "input-row__hidden-control", "chosen": "",
        "label": "Are you 18 years old or over?", "required": True,
        "value": "", "options": ["Yes", "No"],
    }]


def test_checked_nameless_required_radio_group_is_not_required_empty_but_unchecked_is_human_label():
    checked = _required_empty_from_html(
        """
        <fieldset><legend>Do you agree to the privacy policy?</legend>
          <label><input type="radio" required checked> Yes</label>
          <label><input type="radio" required> No</label>
        </fieldset>
        """
    )
    assert checked == []

    unchecked = _required_empty_from_html(
        """
        <fieldset><legend id="agree-question">Do you agree to the privacy policy?</legend>
          <label><input id="oracle_raw_1" type="radio" required> Yes</label>
          <label><input id="oracle_raw_2" type="radio" required> No</label>
        </fieldset>
        """
    )
    assert unchecked == ["Do you agree to the privacy policy?"]
    assert "oracle_raw" not in unchecked[0]


def test_malformed_group_label_equal_to_option_fails_closed_without_invalid_js():
    controls = _extract_from_html(
        """
        <div role="radiogroup">
          <label><input type="radio"> Yes</label>
          <label><input type="radio"> No</label>
        </div>
        """
    )

    assert controls == []


def test_text_input_role_combobox_is_combobox_without_transient_value():
    controls = _extract_from_html(
        """
        <label for="city">City</label>
        <input id="city" role="combobox" value="Transient typed text">
        """
    )

    assert controls[0]["type"] == "combobox"
    assert controls[0]["label"] == "City"
    assert controls[0]["value"] == ""


def test_unknown_stale_answer_id_is_not_mapped_to_only_unrelated_control():
    controls = [{"id": "current-123", "name": "", "label": "Personal website", "value": "", "options": []}]
    answers = [{"id_or_name": "old-456", "answer": "https://example.com"}]

    allowed, blocked = qa.filter_manual_answers(controls, answers)

    assert allowed == []
    assert blocked == [{"id_or_name": "old-456", "answer": "https://example.com"}]


def test_answer_filter_attaches_source_label_when_original_key_matches_control():
    controls = [{"id": "old-456", "name": "", "label": "Personal website", "value": "", "options": []}]
    answers = [{"id_or_name": "old-456", "answer": "https://example.com"}]

    allowed, blocked = qa.filter_manual_answers(controls, answers)

    assert blocked == []
    assert allowed == [{"id_or_name": "old-456", "answer": "https://example.com", "label": "Personal website"}]


def test_fill_answers_rematches_rerendered_nameless_radio_group_by_human_label():
    from playwright.sync_api import sync_playwright

    html = """
    <form>
      <div id="shared">
        <section role="radiogroup" aria-labelledby="q-first">
          <h2 id="q-first">Are you authorized to work in the United States?</h2>
          <label><input type="radio" name="regen_first" value="yes"> Yes</label>
          <label><input type="radio" name="regen_first" value="no"> No</label>
        </section>
        <section role="radiogroup" aria-labelledby="q-second">
          <h2 id="q-second">Will you now or in the future require sponsorship?</h2>
          <label><input type="radio" name="regen_second" value="yes"> Yes</label>
          <label><input type="radio" name="regen_second" value="no"> No</label>
        </section>
      </div>
    </form>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)
            answer = {
                "id_or_name": "stale_second_group_id",
                "label": "Will you now or in the future require sponsorship?",
                "answer": "No",
            }

            filled, failed = qa.fill_answers(page, controls, [answer])

            assert failed == []
            assert filled == ["Will you now or in the future require sponsorship?"]
            assert not page.locator('input[name="regen_first"][value="yes"]').is_checked()
            assert not page.locator('input[name="regen_first"][value="no"]').is_checked()
            assert not page.locator('input[name="regen_second"][value="yes"]').is_checked()
            assert page.locator('input[name="regen_second"][value="no"]').is_checked()
        finally:
            browser.close()


def test_cx_select_harvests_exact_safe_controlled_popup_options_and_commits_selection():
    from playwright.sync_api import sync_playwright

    html = """
    <label for="degree">Degree Program</label>
    <input id="degree" role="combobox" aria-controls="degreePopup" aria-expanded="false" value="">
    <div id="degreePopup" style="display:none">
      <div role="gridcell" class="cx-select__list-item">Computer Science</div>
      <div role="gridcell" class="cx-select__list-item">Economics</div>
    </div>
    <script>
      degree.addEventListener('click', () => { degree.setAttribute('aria-expanded', 'true'); degreePopup.style.display = 'block'; });
      degreePopup.addEventListener('click', (event) => {
        if (event.target.matches('[role=gridcell]')) {
          degree.value = event.target.innerText.trim();
          degree.setAttribute('data-committed-value', degree.value);
          degree.setAttribute('aria-expanded', 'false');
          degreePopup.style.display = 'none';
        }
      });
    </script>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)

            qa.harvest_select_options(page, controls)
            filled, failed = qa.fill_answers(
                page, controls, [{"id_or_name": "degree", "label": "Degree Program", "answer": "Computer Science"}]
            )

            assert controls[0]["options"] == ["Computer Science", "Economics"]
            assert failed == []
            assert filled == ["Degree Program"]
            assert page.locator("#degree").input_value() == "Computer Science"
        finally:
            browser.close()


def test_cx_select_controlled_gridcell_commit_marks_extract_chosen_without_reopening():
    from playwright.sync_api import sync_playwright

    html = """
    <label for="degree">Degree Program</label>
    <input id="degree" role="combobox" aria-controls="degreePopup" aria-expanded="false" value="">
    <div id="degreePopup" style="display:none">
      <div role="gridcell" class="cx-select__list-item">Computer Science</div>
      <div role="gridcell" class="cx-select__list-item">Economics</div>
    </div>
    <script>
      window.openCount = 0;
      degree.addEventListener('click', () => {
        if (degree.getAttribute('aria-expanded') !== 'true') window.openCount += 1;
        degree.setAttribute('aria-expanded', 'true');
        degreePopup.style.display = 'block';
      });
      degreePopup.addEventListener('click', (event) => {
        if (event.target.matches('[role=gridcell]')) {
          degree.value = event.target.innerText.trim();
          degree.setAttribute('aria-expanded', 'false');
          degreePopup.style.display = 'none';
        }
      });
    </script>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)

            qa.harvest_select_options(page, controls)
            filled, failed = qa.fill_answers(
                page, controls, [{"id_or_name": "degree", "label": "Degree Program", "answer": "Computer Science"}]
            )
            fresh_controls = page.evaluate(qa.EXTRACT_JS)
            open_count_after_fill = page.evaluate("window.openCount")
            qa.harvest_select_options(page, fresh_controls)

            assert failed == []
            assert filled == ["Degree Program"]
            assert fresh_controls[0]["chosen"] == "Computer Science"
            assert fresh_controls[0]["value"] == ""
            assert page.evaluate("window.openCount") == open_count_after_fill
        finally:
            browser.close()


def test_cx_select_uses_real_pointer_click_and_survives_transient_untrusted_value():
    from playwright.sync_api import sync_playwright

    html = """
    <label for="degree">Degree Program</label>
    <input id="degree" role="combobox" aria-controls="degreePopup" aria-expanded="false" value="">
    <div id="degreePopup" style="display:none">
      <div role="gridcell" class="cx-select__list-item">Bachelor (BA/BS)</div>
      <div role="gridcell" class="cx-select__list-item">Master (MA/MS)</div>
    </div>
    <script>
      degree.addEventListener('click', () => {
        degree.setAttribute('aria-expanded', 'true');
        degreePopup.style.display = 'block';
      });
      degreePopup.addEventListener('click', (event) => {
        if (!event.target.matches('[role=gridcell]')) return;
        degree.value = event.target.innerText.trim();
        degree.setAttribute('aria-expanded', 'false');
        degreePopup.style.display = 'none';
        if (event.isTrusted) {
          degree.setAttribute('data-committed-value', degree.value);
        } else {
          setTimeout(() => {
            degree.value = '';
            degree.removeAttribute('data-jobhunt-committed-value');
            degree.removeAttribute('data-committed-value');
          }, 400);
        }
      });
    </script>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)
            qa.harvest_select_options(page, controls)

            filled, failed = qa.fill_answers(
                page, controls,
                [{"id_or_name": "degree", "label": "Degree Program", "answer": "Bachelor (BA/BS)"}],
            )
            page.wait_for_timeout(700)

            assert failed == []
            assert filled == ["Degree Program"]
            assert page.locator("#degree").input_value() == "Bachelor (BA/BS)"
            assert page.locator("#degree").get_attribute("data-committed-value") == "Bachelor (BA/BS)"
        finally:
            browser.close()


def test_cx_select_fill_fails_closed_for_ambiguous_or_missing_scoped_options():
    from playwright.sync_api import sync_playwright

    html = """
    <label for="degree">Degree Program</label>
    <input id="degree" role="combobox" aria-controls="degreePopup" aria-expanded="false" value="">
    <div id="degreePopup" style="display:none">
      <div role="gridcell" class="cx-select__list-item">Computer Science BA</div>
      <div role="gridcell" class="cx-select__list-item">Computer Science BS</div>
    </div>
    <script>
      degree.addEventListener('click', () => { degree.setAttribute('aria-expanded', 'true'); degreePopup.style.display = 'block'; });
      degreePopup.addEventListener('click', (event) => {
        if (event.target.matches('[role=gridcell]')) degree.value = event.target.innerText.trim();
      });
    </script>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)
            qa.harvest_select_options(page, controls)

            filled, failed = qa.fill_answers(
                page, controls, [{"id_or_name": "degree", "label": "Degree Program", "answer": "Computer Science"}]
            )

            assert filled == []
            assert failed == ["Degree Program"]
            assert page.locator("#degree").input_value() == ""
        finally:
            browser.close()


def test_cx_select_rejects_unsafe_aria_controls_and_never_reads_unscoped_gridcells():
    from playwright.sync_api import sync_playwright

    html = """
    <label for="degree">Degree Program</label>
    <input id="degree" role="combobox" aria-controls="degree popup" aria-expanded="false" value="">
    <div role="gridcell" class="cx-select__list-item">Computer Science</div>
    <script>degree.addEventListener('click', () => degree.setAttribute('aria-expanded', 'true'));</script>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)
            qa.harvest_select_options(page, controls)

            filled, failed = qa.fill_answers(
                page, controls, [{"id_or_name": "degree", "label": "Degree Program", "answer": "Computer Science"}]
            )

            assert controls[0]["options"] == []
            assert filled == []
            assert failed == ["Degree Program"]
            assert page.locator("#degree").input_value() == ""
        finally:
            browser.close()


def test_cx_select_transient_typed_text_is_not_success_without_committed_option():
    from playwright.sync_api import sync_playwright

    html = """
    <label for="degree">Degree Program</label>
    <input id="degree" role="combobox" aria-controls="degreePopup" aria-expanded="false" value="">
    <div id="degreePopup" style="display:none"></div>
    <script>
      degree.addEventListener('click', () => { degree.setAttribute('aria-expanded', 'true'); degreePopup.style.display = 'block'; });
      degree.addEventListener('input', () => { degree.value = 'Computer Science'; });
    </script>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)

            filled, failed = qa.fill_answers(
                page, controls, [{"id_or_name": "degree", "label": "Degree Program", "answer": "Computer Science"}]
            )

            assert filled == []
            assert failed == ["Degree Program"]
        finally:
            browser.close()


def test_exact_outside_work_question_is_hard_blocked_but_broader_work_questions_are_not():
    blocked_control = {"id": "outside", "name": "", "label": "Outside work", "value": "", "options": ["Yes", "No"]}
    allowed_control = {"id": "auth", "name": "", "label": "Are you authorized to work in the United States?", "value": "", "options": ["Yes", "No"]}

    allowed, blocked = qa.filter_manual_answers(
        [blocked_control, allowed_control],
        [{"id_or_name": "outside", "answer": "No"}, {"id_or_name": "auth", "answer": "Yes"}],
        profile_text="",
        approved_answers={},
    )

    assert [a["id_or_name"] for a in blocked] == ["outside"]
    assert [a["id_or_name"] for a in allowed] == ["auth"]


def test_fill_answers_scopes_nameless_radio_to_exact_question_wrapper_not_shared_ancestor():
    from playwright.sync_api import sync_playwright

    html = """
    <div class="questionnaire">
      <div class="question-block" role="radiogroup" aria-labelledby="q1">
        <div id="q1">Are you authorized to work in the United States?</div>
        <label><input type="radio" required> Yes</label>
        <label><input type="radio" required> No</label>
      </div>
      <div class="question-block" role="radiogroup" aria-labelledby="q2">
        <div id="q2">Will you now or in the future require sponsorship?</div>
        <label><input type="radio" required> Yes</label>
        <label><input type="radio" required> No</label>
      </div>
    </div>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(html)
            controls = page.evaluate(qa.EXTRACT_JS)
            filled, failed = qa.fill_answers(page, controls, [{
                "id_or_name": "Will you now or in the future require sponsorship?",
                "label": "Will you now or in the future require sponsorship?",
                "answer": "No",
            }])

            assert failed == []
            assert filled == ["Will you now or in the future require sponsorship?"]
            radios = page.locator('input[type="radio"]')
            assert not radios.nth(0).is_checked()
            assert not radios.nth(1).is_checked()
            assert not radios.nth(2).is_checked()
            assert radios.nth(3).is_checked()
        finally:
            browser.close()


def test_nameless_required_radio_groups_under_questionnaire_are_separate_for_empty_and_extract():
    html = """
    <div class="questionnaire">
      <div class="question-block" role="radiogroup" aria-labelledby="q1">
        <div id="q1">Are you authorized to work in the United States?</div>
        <label><input type="radio" required checked> Yes</label>
        <label><input type="radio" required> No</label>
      </div>
      <div class="question-block" role="radiogroup" aria-labelledby="q2">
        <div id="q2">Will you now or in the future require sponsorship?</div>
        <label><input type="radio" required> Yes</label>
        <label><input type="radio" required> No</label>
      </div>
    </div>
    """

    required_empty = _required_empty_from_html(html)
    controls = _extract_from_html(html)

    assert required_empty == ["Will you now or in the future require sponsorship?"]
    assert [control["label"] for control in controls] == [
        "Are you authorized to work in the United States?",
        "Will you now or in the future require sponsorship?",
    ]


def test_named_single_consent_checkbox_is_extracted_when_label_matches_option():
    controls = _extract_from_html(
        """
        <label><input type="checkbox" name="consent" required> I certify that the information is accurate</label>
        """
    )

    assert controls == [{
        "id": "", "name": "consent", "tag": "input", "type": "group-checkbox", "cls": "",
        "chosen": "", "label": "I certify that the information is accurate",
        "required": True, "value": "", "options": ["I certify that the information is accurate"],
    }]


def test_committed_collapsed_cx_select_is_chosen_and_skipped_by_get_answers(monkeypatch):
    controls = _extract_from_html(
        """
        <label for="degree">Degree Program</label>
        <input id="degree" role="combobox" aria-expanded="false" value="Computer Science" data-committed-value="Computer Science">
        """
    )

    assert controls[0]["type"] == "combobox"
    assert controls[0]["value"] == ""
    assert controls[0]["chosen"] == "Computer Science"
    monkeypatch.setattr(qa, "_model_answers", lambda model_controls, company_context="": (_ for _ in ()).throw(AssertionError("committed control reopened")))
    assert qa.get_answers(controls) == []


def test_expanded_cx_select_typed_text_stays_uncommitted_unanswered():
    controls = _extract_from_html(
        """
        <label for="degree">Degree Program</label>
        <input id="degree" role="combobox" aria-expanded="true" value="Computer Science">
        """
    )

    assert controls[0]["type"] == "combobox"
    assert controls[0]["value"] == ""
    assert controls[0]["chosen"] == ""
