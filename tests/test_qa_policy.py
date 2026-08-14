import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))
import qa  # noqa: E402
import greenhouse  # noqa: E402
import smartrecruiters  # noqa: E402
import workday  # noqa: E402


class QaManualPolicyTest(unittest.TestCase):
    APPROVED = {
        "version": 1,
        "identity": {
            "date_of_birth": "06/01/2006",
            "pronouns": "He/Him",
            "gender": "male",
            "disability": {"current": False, "history": False},
        },
        "preferences": {
            "hybrid": True,
            "relocation_assistance_required": False,
            "compensation_policy": "Use an employer-published range; otherwise open / market rate.",
        },
        "education": {
            "expected_graduation_month": "June",
            "expected_graduation_year": "2028",
            "exact_graduation_date": "06/01/2028",
        },
        "current_offers": [
            {"company": "Soren", "deadline_month": "September 2026", "deadline": None}
        ],
        "long_form_answers": [
            {
                "key": "non_computer_system_hack",
                "match_all": ["most successfully hacked", "non-computer system"],
                "answer": "I organized a campus event by recruiting aligned partners and using an existing event process.",
            },
            {
                "key": "most_impressive_achievement",
                "match_all": ["most impressive thing", "built or achieved"],
                "answer": "I reached USACO Platinum after starting competitive programming in high school.",
            },
            {
                "key": "things_built",
                "match_all": ["built before", "include urls"],
                "answer": "I built Bruno's Dictionary and several workflow tools.",
            },
            {
                "key": "competitions_awards_papers",
                "match_all": ["competitions", "awards", "papers"],
                "answer": "USACO Platinum, AIME 4x, TartanHacks Grand Prize.",
            },
            {
                "key": "test_scores",
                "match_all": ["relevant or impressive test scores"],
                "answer": "1570 SAT and 1520 PSAT.",
            },
            {
                "key": "entrepreneurship_programs_clubs",
                "match_all": ["entrepreneurship programs", "clubs", "hacker houses"],
                "answer": "Hack@Brown, Brown-RISD Game Developers, Full Stack@Brown, Brown Product Management.",
            },
        ],
        "company_facts": {
            "Sentry": {"used_product": True},
            "LPL Financial": {
                "prior_employment": False,
                "referral": False,
                "prior_interview_or_application": False,
                "licenses_or_exams": False,
            },
            "Deloitte": {"household_employment": False},
        },
    }

    def test_smartrecruiters_intro_does_not_hardcode_a_grad_year(self):
        message = smartrecruiters._hiring_team_message(qa.PROFILE)
        self.assertNotIn("'27", message)
        self.assertNotIn("May 2027", message)
        self.assertNotIn("May 2028", message)
        self.assertIn("Brown University", message)

    def test_filter_blocks_explicit_user_fact_preferences(self):
        controls = [
            {"id": "dob", "name": "", "label": "Date of birth", "value": ""},
            {"id": "comp", "name": "", "label": "Desired salary", "value": ""},
            {"id": "offer", "name": "", "label": "Do you have an offer deadline?", "value": ""},
            {"id": "prod", "name": "", "label": "Have you used our product before?", "value": ""},
            {"id": "ref", "name": "", "label": "Were you referred by a current employee?", "value": ""},
            {"id": "nc", "name": "", "label": "Are you bound by a non-compete?", "value": ""},
            {"id": "clear", "name": "", "label": "What security clearance do you hold?", "value": ""},
            {"id": "sched", "name": "", "label": "List your exact work schedule and travel percentage", "value": ""},
            {"id": "dis", "name": "", "label": "Disability history", "value": ""},
            {"id": "pronouns", "name": "", "label": "Preferred pronouns", "value": ""},
            {"id": "used", "name": "", "label": "Have you ever used Sentry before?", "value": ""},
            {"id": "hybrid", "name": "", "label": "Are you willing to join us in office 3 days a week?", "value": ""},
            {"id": "grad", "name": "", "label": "Anticipated graduation date", "kind": "date", "hasDay": True, "value": ""},
            {"id": "high-school", "name": "", "label": "Where did you attend high school?", "value": ""},
            {"id": "interview", "name": "", "label": "Have you previously interviewed at LPL?", "value": ""},
            {"id": "finra", "name": "", "label": "Do you hold any FINRA licenses?", "value": ""},
            {"id": "household", "name": "", "label": "Was a member of your household employed by Deloitte?", "value": ""},
            {"id": "media", "name": "", "label": "What are you reading right now?", "value": ""},
            {"id": "allowed", "name": "", "label": "Why are you interested in this internship?", "value": ""},
        ]
        answers = [{"id_or_name": c["id"], "answer": "model answer"} for c in controls]

        allowed, blocked = qa.filter_manual_answers(controls, answers, profile_text="")

        self.assertEqual(["allowed"], [a["id_or_name"] for a in allowed])
        self.assertEqual({c["id"] for c in controls if c["id"] != "allowed"}, {a["id_or_name"] for a in blocked})

    def test_interview_and_finra_facts_do_not_overreach(self):
        approved = {
            "company_facts": {
                "Jane Street": {
                    "prior_interview": False,
                    "prior_application": True,
                },
                "Chicago Trading Company": {
                    "finra_registered": False,
                    "finra_licenses": True,
                    "securities_exam_planned": True,
                },
            },
        }
        yes_no = ["Yes", "No"]
        jane = [
            {"id": "interview", "name": "", "label": "Have you interviewed with Jane Street before?", "options": yes_no, "value": ""},
            {"id": "applied", "name": "", "label": "Have you applied to Jane Street before?", "options": yes_no, "value": ""},
        ]
        self.assertEqual(
            [
                {"id_or_name": "interview", "answer": "No"},
                {"id_or_name": "applied", "answer": "Yes"},
            ],
            qa.explicit_approved_answers(
                jane, company_context="Jane Street", approved_answers=approved,
            ),
        )
        finra = [
            {"id": "registered", "name": "", "label": "Are you currently registered with FINRA?", "options": yes_no, "value": ""},
            {"id": "license", "name": "", "label": "Do you hold any FINRA licenses?", "options": yes_no, "value": ""},
            {"id": "exam", "name": "", "label": "Do you plan to take the SIE exam?", "options": yes_no, "value": ""},
        ]
        self.assertEqual(
            [
                {"id_or_name": "registered", "answer": "No"},
                {"id_or_name": "license", "answer": "Yes"},
                {"id_or_name": "exam", "answer": "Yes"},
            ],
            qa.explicit_approved_answers(
                finra,
                company_context="Chicago Trading Company",
                approved_answers=approved,
            ),
        )

    def test_age_and_current_location_use_approved_profile_facts(self):
        controls = [
            {"id": "age", "name": "", "label": "Are you 18 years of age or older?", "options": ["Yes", "No"], "value": ""},
            {"id": "location", "name": "", "label": "Current location", "value": ""},
            {"id": "university-location", "name": "", "label": "Please select the location of your current university", "options": ["Providence, RI", "Boston, MA"], "value": ""},
        ]
        self.assertEqual(
            [
                {"id_or_name": "age", "answer": "Yes"},
                {"id_or_name": "location", "answer": "Providence, RI"},
                {"id_or_name": "university-location", "answer": "Providence, RI"},
            ],
            qa.explicit_approved_answers(controls, approved_answers=self.APPROVED),
        )

    def test_profile_answers_education_citizenship_tests_and_full_time_date(self):
        controls = [
            {"id": "discipline", "name": "", "label": "Undergrad Discipline(s)", "options": ["Computer Science", "Physics"], "value": ""},
            {"id": "diploma", "name": "", "label": "High School Diploma", "options": ["Yes", "No"], "value": ""},
            {"id": "test", "name": "", "label": "Select your Standardized Test score type", "options": ["ACT", "SAT"], "value": ""},
            {"id": "citizenship", "name": "", "label": "Please select the country where you hold citizenship / permanent residence", "options": ["Canada", "United States of America"], "value": ""},
            {"id": "full-time", "name": "", "label": "When will you be available to work as a full-time, permanent employee?", "options": ["August 2027", "August 2028"], "value": ""},
            {"id": "offers", "name": "", "label": "Do you currently have any offers from other firms?", "options": ["Yes", "No"], "value": ""},
        ]
        self.assertEqual(
            [
                {"id_or_name": "discipline", "answer": "Computer Science"},
                {"id_or_name": "diploma", "answer": "Yes"},
                {"id_or_name": "test", "answer": "SAT"},
                {"id_or_name": "citizenship", "answer": "United States of America"},
                {"id_or_name": "full-time", "answer": "August 2028"},
                {"id_or_name": "offers", "answer": "Yes"},
            ],
            qa.explicit_approved_answers(controls, approved_answers=self.APPROVED),
        )

    def test_greenhouse_upload_scan_accepts_a_populated_file_input(self):
        source = Path(greenhouse.__file__).read_text()
        self.assertIn("fileInput?.files?.length", source)
        self.assertIn("uploadLabel.replace", source)

    def test_semester_graduation_and_organization_membership_use_approved_facts(self):
        approved = {
            **self.APPROVED,
            "long_form_answers": [{
                "key": "university_organizations",
                "match_all": ["currently a member", "university organizations"],
                "answer": "Hack@Brown, Brown-RISD Game Developers, Full Stack@Brown, and Brown Product Management.",
            }],
        }
        controls = [
            {
                "id": "graduation",
                "label": "Please select your expected graduation month and year for your current studies.",
                "options": ["Fall 2027", "Spring 2028", "Fall 2028"],
                "value": "",
            },
            {
                "id": "organizations",
                "label": "Are you currently a member of any university organizations, such as clubs or societies?",
                "options": ["Yes", "No"],
                "value": "",
            },
        ]
        rendered = qa.explicit_approved_answers(controls, approved_answers=approved)
        self.assertEqual(
            [
                {"id_or_name": "graduation", "answer": "Spring 2028"},
                {"id_or_name": "organizations", "answer": "Yes"},
            ],
            rendered,
        )
        for control, answer in zip(controls, ("Spring 2028", "Yes")):
            self.assertFalse(qa.answer_requires_manual(
                control, answer, approved_answers=approved,
            ))

    def test_optional_recruiting_marketing_defaults_to_no(self):
        control = {
            "id": "marketing",
            "label": "Receive recruitment marketing communications?",
            "options": ["Yes", "No"],
            "value": "",
        }

        rendered = qa.explicit_approved_answers(
            [control], approved_answers=self.APPROVED,
        )
        self.assertEqual(
            [{"id_or_name": "marketing", "answer": "No"}],
            rendered,
        )
        self.assertFalse(
            qa.answer_requires_manual(
                control, "No", approved_answers=self.APPROVED,
            )
        )
        self.assertTrue(
            qa.answer_requires_manual(
                control, "Yes", approved_answers=self.APPROVED,
            )
        )

    def test_optional_other_detail_cannot_be_invented(self):
        controls = [
            {"id": "other", "label": "If other, please specify", "required": False, "value": ""},
            {"id": "selected-other", "label": "If you selected Other, describe it", "required": False, "value": ""},
        ]
        answers = [
            {"id_or_name": "other", "answer": "Framewise Health"},
            {"id_or_name": "selected-other", "answer": "Coding competition"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED,
        )

        self.assertEqual([], allowed)
        self.assertEqual({"other", "selected-other"}, {answer["id_or_name"] for answer in blocked})

    def test_active_other_school_detail_can_use_the_profile_institution(self):
        control = {
            "id": "other-school",
            "label": "If you selected other, please specify which one",
            "required": True,
            "value": "",
        }
        self.assertFalse(
            qa.answer_requires_manual(
                control, "Brown University", approved_answers=self.APPROVED,
            )
        )
        self.assertTrue(
            qa.answer_requires_manual(
                control, "Made Up University", approved_answers=self.APPROVED,
            )
        )

    def test_greenhouse_required_scan_ignores_hidden_conditional_controls(self):
        source = Path(greenhouse.__file__).read_text()
        self.assertIn("el.offsetParent === null", source)
        self.assertIn("el.getClientRects().length === 0", source)
        self.assertIn("el.closest('[hidden], [aria-hidden=\"true\"]')", source)

    def test_greenhouse_required_scan_ignores_inactive_other_branch(self):
        source = Path(greenhouse.__file__).read_text()
        self.assertIn("const otherSelected", source)
        self.assertIn("&& !otherSelected", source)
        self.assertIn("selected?|chose|choose", source)

    def test_hrt_timeline_and_discovery_answers_are_deterministic(self):
        controls = [
            {
                "id": "eligible",
                "label": "Based on your expected graduation date, when is the earliest you are eligible to begin full-time employment at HRT?",
                "options": ["August 2027", "February 2028", "August 2028", "February 2029"],
            },
            {
                "id": "first-heard",
                "label": "When did you first hear about HRT?",
                "options": ["High School", "University Program", "Graduate Program"],
            },
            {
                "id": "source",
                "label": "How did you hear about HRT?",
                "options": ["Coding Competition", "University Job Board", "HRT Job Board", "Other"],
            },
        ]
        rendered = qa.explicit_approved_answers(
            controls, approved_answers=self.APPROVED,
        )
        self.assertEqual(
            {
                "eligible": "August 2028",
                "first-heard": "University Program",
                "source": "HRT Job Board",
            },
            {item["id_or_name"]: item["answer"] for item in rendered},
        )
        self.assertFalse(
            qa.answer_requires_manual(
                controls[0], "August 2028", approved_answers=self.APPROVED,
            )
        )
        self.assertTrue(
            qa.answer_requires_manual(
                controls[0], "February 2028", approved_answers=self.APPROVED,
            )
        )

    def test_greenhouse_school_and_combined_grad_menu_use_known_profile_facts(self):
        controls = [
            {
                "id": "school",
                "label": "School*",
                "options": ["Aalborg University", "Aalto University", "Aarhus University"],
                "value": "",
            },
            {
                "id": "graduation",
                "label": "What is your expected graduation month & year?*",
                "options": [
                    "Already graduated", "Jan - Aug 2026", "Sept - Dec 2026",
                    "Jan - April 2027", "May - Aug 2027", "Aug 2027 or later",
                ],
                "value": "",
            },
        ]

        rendered = qa.explicit_approved_answers(
            controls, approved_answers=self.APPROVED,
        )

        self.assertEqual(
            {
                "school": "Brown University",
                "graduation": "Aug 2027 or later",
            },
            {item["id_or_name"]: item["answer"] for item in rendered},
        )
        self.assertFalse(
            qa.answer_requires_manual(
                controls[1], "Aug 2027 or later", approved_answers=self.APPROVED,
            )
        )

    def test_demographics_use_explicit_gender_and_decline_unknown_answers(self):
        controls = [
            {"id": "gender", "label": "What is your gender?", "options": ["Woman", "Man", "Non-binary", "I don't wish to answer"]},
            {"id": "race", "label": "What is your race/ethnicity?", "options": ["East Asian", "White", "I don't wish to answer"]},
            {"id": "hispanic", "label": "Are you Hispanic/Latino?", "options": ["Yes", "No", "I don't wish to answer"]},
            {"id": "orientation", "label": "How would you describe your sexual orientation?", "options": ["Straight", "Gay", "I don't wish to answer"]},
            {"id": "transgender", "label": "Do you identify as transgender?", "options": ["Yes", "No", "I don't wish to answer"]},
            {"id": "veteran", "label": "Are you a veteran?", "options": ["Yes", "No", "I don't wish to answer"]},
        ]
        rendered = qa.explicit_approved_answers(
            controls, approved_answers=self.APPROVED,
        )
        self.assertEqual(
            {
                "gender": "Man",
                "race": "I don't wish to answer",
                "hispanic": "I don't wish to answer",
                "orientation": "I don't wish to answer",
                "transgender": "I don't wish to answer",
                "veteran": "I don't wish to answer",
            },
            {item["id_or_name"]: item["answer"] for item in rendered},
        )
        self.assertTrue(
            qa.answer_requires_manual(
                controls[1], "East Asian", approved_answers=self.APPROVED,
            )
        )
        self.assertTrue(
            qa.answer_requires_manual(
                controls[2], "No", approved_answers=self.APPROVED,
            )
        )
        self.assertTrue(
            qa.answer_requires_manual(
                controls[3], "No", approved_answers=self.APPROVED,
            )
        )
        self.assertTrue(
            qa.answer_requires_manual(
                controls[4], "No", approved_answers=self.APPROVED,
            )
        )
        self.assertTrue(
            qa.answer_requires_manual(
                controls[5], "No", approved_answers=self.APPROVED,
            )
        )

    def test_react_select_extraction_tracks_multi_value_chips(self):
        self.assertIn(".select__multi-value", qa.EXTRACT_JS)
        self.assertIn("[class*=multiValue]", qa.EXTRACT_JS)

    def test_disability_allowed_when_profile_has_explicit_text(self):
        control = {"id": "dis", "label": "Voluntary disability status", "value": ""}
        self.assertTrue(qa.answer_requires_manual(control, "No", profile_text=""))
        self.assertFalse(qa.answer_requires_manual(control, "No", profile_text="disability: no"))

    def test_user_approved_answers_are_allowed_but_missing_deadlines_stay_manual(self):
        controls = [
            {"id": "dob", "label": "Date of birth", "value": ""},
            {"id": "pronouns", "label": "Preferred pronouns", "value": ""},
            {"id": "product", "label": "Have you ever used Sentry before?", "value": ""},
            {"id": "prior", "label": "Have you worked for LPL Financial as an employee?", "value": ""},
            {"id": "hybrid", "label": "Can you work in-office 3 days a week?", "value": ""},
            {"id": "offer", "label": "Do you have an outstanding offer?", "value": ""},
            {"id": "deadline", "label": "What is your offer deadline?", "value": ""},
            {"id": "comp", "label": "Desired compensation", "value": "", "options": []},
        ]
        answers = [
            {"id_or_name": "dob", "answer": "06/01/2006"},
            {"id_or_name": "pronouns", "answer": "He/Him"},
            {"id_or_name": "product", "answer": "Yes"},
            {"id_or_name": "prior", "answer": "No"},
            {"id_or_name": "hybrid", "answer": "Yes"},
            {"id_or_name": "offer", "answer": "Yes, Soren"},
            {"id_or_name": "deadline", "answer": "No deadline"},
            {"id_or_name": "comp", "answer": "$50/hour"},
        ]
        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )
        self.assertEqual(
            {"dob", "pronouns", "product", "prior", "hybrid", "offer"},
            {answer["id_or_name"] for answer in allowed},
        )
        self.assertEqual(
            {"deadline", "comp"},
            {answer["id_or_name"] for answer in blocked},
        )

    def test_user_authored_long_form_answers_render_exactly_without_a_model(self):
        controls = [
            {
                "id": "hack",
                "label": "Tell us about a time you most successfully hacked a non-computer system to your advantage.",
                "value": "",
            },
            {
                "id": "achievement",
                "label": "Tell us in one or two sentences about the most impressive thing other than this startup that you have built or achieved.",
                "value": "",
            },
            {
                "id": "built",
                "label": "Tell us about things you've built before. Include URLs if possible.",
                "value": "",
            },
            {
                "id": "awards",
                "label": "List any competitions/awards you have won, or papers you've published.",
                "value": "",
            },
            {
                "id": "scores",
                "label": "List any relevant or impressive test scores.",
                "value": "",
            },
            {
                "id": "programs",
                "label": "List any entrepreneurship programs, clubs, or hacker houses you have participated in.",
                "value": "",
            },
            {
                "id": "similar-but-not-approved",
                "label": "Tell us about a computer system you successfully hacked.",
                "value": "",
            },
        ]

        rendered = qa.explicit_approved_answers(
            controls,
            approved_answers=self.APPROVED,
        )

        self.assertEqual(
            {
                "hack": "I organized a campus event by recruiting aligned partners and using an existing event process.",
                "achievement": "I reached USACO Platinum after starting competitive programming in high school.",
                "built": "I built Bruno's Dictionary and several workflow tools.",
                "awards": "USACO Platinum, AIME 4x, TartanHacks Grand Prize.",
                "scores": "1570 SAT and 1520 PSAT.",
                "programs": "Hack@Brown, Brown-RISD Game Developers, Full Stack@Brown, Brown Product Management.",
            },
            {answer["id_or_name"]: answer["answer"] for answer in rendered},
        )

    def test_only_matching_long_form_answers_enter_the_model_context(self):
        controls = [{
            "id": "hack",
            "label": "Tell us about a time you most successfully hacked a non-computer system.",
            "value": "",
        }]

        relevant = qa.relevant_application_answers(
            controls,
            approved=self.APPROVED,
        )

        self.assertEqual(
            ["non_computer_system_hack"],
            [entry["key"] for entry in relevant["long_form_answers"]],
        )

    def test_month_only_offer_deadline_is_allowed_but_day_is_not_invented(self):
        controls = [
            {"id": "month", "label": "What is your offer deadline?", "value": ""},
            {
                "id": "exact",
                "label": "What is your offer deadline?",
                "kind": "date",
                "hasDay": True,
                "value": "",
            },
        ]
        answers = [
            {"id_or_name": "month", "answer": "September 2026"},
            {"id_or_name": "exact", "answer": "09/15/2026"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )

        self.assertEqual(["month"], [answer["id_or_name"] for answer in allowed])
        self.assertEqual(["exact"], [answer["id_or_name"] for answer in blocked])

    def test_approved_boolean_must_match_and_company_context_is_supported(self):
        controls = [
            {
                "id": "referral-no",
                "label": "Were you referred by a current employee?",
                "company_context": "lplfinancial",
                "value": "",
            },
            {
                "id": "referral-wrong",
                "label": "Were you referred by a current employee?",
                "company_context": "lplfinancial",
                "value": "",
            },
            {
                "id": "household",
                "label": "Was a member of your household employed by Deloitte?",
                "company_context": "lplfinancial",
                "value": "",
            },
        ]
        answers = [
            {"id_or_name": "referral-no", "answer": "No"},
            {"id_or_name": "referral-wrong", "answer": "Yes"},
            {"id_or_name": "household", "answer": "No"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )

        self.assertEqual(
            {"referral-no", "household"},
            {answer["id_or_name"] for answer in allowed},
        )
        self.assertEqual(["referral-wrong"], [answer["id_or_name"] for answer in blocked])

    def test_graduation_month_year_and_approved_estimated_day_must_match(self):
        controls = [
            {"id": "right", "label": "Graduation date", "value": ""},
            {"id": "wrong", "label": "Graduation date", "value": ""},
            {"id": "day", "label": "Graduation date", "hasDay": True, "value": ""},
        ]
        answers = [
            {"id_or_name": "right", "answer": "June 2028"},
            {"id_or_name": "wrong", "answer": "May 2027"},
            {"id_or_name": "day", "answer": "06/01/2028"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )

        self.assertEqual(
            {"right", "day"},
            {answer["id_or_name"] for answer in allowed},
        )
        self.assertEqual(["wrong"], [answer["id_or_name"] for answer in blocked])

    def test_explicit_facts_render_without_a_model_including_approved_estimate(self):
        controls = [
            {
                "id": "referral",
                "label": "Were you referred by a current employee?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "grad",
                "label": "Graduation date",
                "kind": "date",
                "hasDay": False,
                "value": "",
            },
            {
                "id": "grad-day",
                "label": "Graduation date",
                "kind": "date",
                "hasDay": True,
                "value": "",
            },
            {
                "id": "deadline",
                "label": "Offer deadline",
                "value": "",
            },
        ]

        rendered = qa.explicit_approved_answers(
            controls,
            company_context="lplfinancial",
            approved_answers=self.APPROVED,
        )

        self.assertEqual(
            {
                "referral": "No",
                "grad": "06/2028",
                "grad-day": "06/01/2028",
                "deadline": "September 2026",
            },
            {answer["id_or_name"]: answer["answer"] for answer in rendered},
        )

    def test_lpl_factual_answers_render_without_model_guesses(self):
        controls = [
            {
                "id": "education",
                "label": "What is your highest level of education?",
                "options": ["High school diploma", "Some college, no degree", "Bachelor’s degree"],
            },
            {
                "id": "major",
                "label": "What is your current major/area of study?",
                "options": ["Accounting", "Computer Science", "Economics"],
            },
            {
                "id": "auth",
                "label": "Are you legally authorized to work in the United States for any employer?",
                "options": ["Yes", "No"],
            },
            {
                "id": "sponsor",
                "label": "Will you now or in the future require immigration sponsorship by our company?",
                "options": ["Yes", "No"],
            },
            {
                "id": "former",
                "label": "Are you a former LPL employee or contingent worker?",
                "options": ["Yes", "No"],
            },
            {
                "id": "finra",
                "label": "Do you hold any FINRA licenses?",
                "options": ["Yes - please indicate below", "No"],
            },
            {
                "id": "local",
                "label": "Are you local to the area in which this job has been advertised?",
                "options": [
                    "Yes",
                    "No - I am willing to relocate & I do not require relocation assistance.",
                ],
            },
            {
                "id": "locations",
                "label": "If offered this position, which location(s) would you be open to working in? Please select all that apply.",
                "kind": "checkgroup",
                "options": ["Austin, TX", "New York, NY", "Washington, DC"],
            },
        ]

        rendered = qa.explicit_approved_answers(
            controls,
            company_context="lplfinancial",
            approved_answers=self.APPROVED,
        )
        answers = {item["id_or_name"]: item["answer"] for item in rendered}

        self.assertEqual("Some college, no degree", answers["education"])
        self.assertEqual("Computer Science", answers["major"])
        self.assertEqual("Yes", answers["auth"])
        self.assertEqual("No", answers["sponsor"])
        self.assertEqual("No", answers["former"])
        self.assertEqual("No", answers["finra"])
        self.assertEqual(controls[-2]["options"][1], answers["local"])
        self.assertEqual(controls[-1]["options"], answers["locations"])

        allowed, blocked = qa.filter_manual_answers(
            [dict(controls[-2], company_context="lplfinancial")],
            [{"id_or_name": "local", "answer": controls[-2]["options"][1]}],
            approved_answers=self.APPROVED,
        )
        self.assertEqual(["local"], [item["id_or_name"] for item in allowed])
        self.assertEqual([], blocked)

    def test_relocation_assistance_policy_allows_no_and_rejects_yes(self):
        controls = [
            {
                "id": key,
                "label": "Will you require relocation assistance?",
                "options": ["Yes", "No"],
            }
            for key in ("right", "wrong")
        ]

        rendered = qa.explicit_approved_answers(
            controls,
            approved_answers=self.APPROVED,
        )
        self.assertEqual(
            {"right": "No", "wrong": "No"},
            {answer["id_or_name"]: answer["answer"] for answer in rendered},
        )

        allowed, blocked = qa.filter_manual_answers(
            controls,
            [
                {"id_or_name": "right", "answer": "No"},
                {"id_or_name": "wrong", "answer": "Yes"},
            ],
            approved_answers=self.APPROVED,
        )
        self.assertEqual(["right"], [answer["id_or_name"] for answer in allowed])
        self.assertEqual(["wrong"], [answer["id_or_name"] for answer in blocked])

    def test_former_named_company_employee_answer_is_policy_checked(self):
        controls = [
            {
                "id": key,
                "label": "Are you a former LPL employee or contingent worker?",
                "company_context": "lplfinancial",
                "options": ["Yes", "No"],
            }
            for key in ("former", "former-wrong")
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls,
            [
                {"id_or_name": "former", "answer": "No"},
                {"id_or_name": "former-wrong", "answer": "Yes"},
            ],
            approved_answers=self.APPROVED,
        )

        self.assertEqual(["No"], [answer["answer"] for answer in allowed])
        self.assertEqual(["Yes"], [answer["answer"] for answer in blocked])

    def test_checkgroup_targets_preserve_every_requested_location(self):
        labels = ["Austin, TX", "New York, NY", "Washington, DC"]
        self.assertEqual(labels, workday._checkgroup_targets(labels, labels))
        self.assertEqual([], workday._checkgroup_targets(["Austin, TX", "Paris"], labels))

    def test_only_relevant_sensitive_answers_enter_the_model_prompt(self):
        context = qa.relevant_application_answers(
            [{"label": "Preferred pronouns"}], approved=self.APPROVED
        )
        self.assertEqual("He/Him", context["identity"]["pronouns"])
        self.assertNotIn("date_of_birth", context["identity"])
        self.assertNotIn("current_offers", context)

    def test_high_school_is_blocked_until_explicitly_approved(self):
        control = {"id": "school", "label": "Where did you attend high school?", "value": ""}
        self.assertTrue(
            qa.answer_requires_manual(
                control,
                "North America",
                approved_answers=self.APPROVED,
            )
        )

        approved = json.loads(json.dumps(self.APPROVED))
        approved["education"]["high_school"] = "Example High School, Example City"
        rendered = qa.explicit_approved_answers([control], approved_answers=approved)
        self.assertEqual(
            [{"id_or_name": "school", "answer": "Example High School, Example City"}],
            rendered,
        )
        self.assertFalse(
            qa.answer_requires_manual(
                control,
                "Example High School, Example City",
                approved_answers=approved,
            )
        )

    def test_texas_high_school_maps_to_north_america_picker(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["education"]["high_school"] = (
            "Plano West Senior High School, Plano, Texas"
        )
        control = {
            "id": "school-region",
            "label": "Where did you attend high school/secondary school?",
            "value": "",
            "options": [
                "North America", "South America", "Europe", "Asia", "Africa", "Australia"
            ],
        }

        self.assertEqual(
            [{"id_or_name": "school-region", "answer": "North America"}],
            qa.explicit_approved_answers([control], approved_answers=approved),
        )
        self.assertFalse(
            qa.answer_requires_manual(
                control,
                "North America",
                approved_answers=approved,
            )
        )

    def test_newly_confirmed_defaults_render_and_reject_contradictions(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["education"].update({
            "high_school": "Plano West Senior High School, Plano, Texas",
            "high_school_graduation_year": "2024",
        })
        approved["legal"] = {
            "non_compete_or_conflict": False,
            "notice_period": "None",
            "valid_drivers_license": True,
        }
        approved["professional"] = {"publications": []}
        approved["company_facts"].update({
            "Nextiva": {"prior_employment": False, "fully_onsite": True},
            "Crowe": {"prior_employment": False},
            "Akuna Capital": {
                "prior_application": False,
                "prior_interview_or_application": False,
            },
        })
        controls = [
            {
                "id": "high-school-year",
                "label": "What year did you graduate high school?",
                "value": "",
            },
            {
                "id": "driver",
                "label": "Do you have a valid driver's license?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "agreement",
                "label": "Do you have any agreement with your current employer or any other employer that restricts your work?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "notice",
                "label": "Non-compete/Notice period comments",
                "value": "",
            },
            {
                "id": "publications",
                "label": "Please provide a list of your publications, ranked.",
                "value": "",
            },
            {
                "id": "nextiva-employee",
                "label": "Are you a current or former Nextiva employee?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "nextiva-onsite",
                "label": "This role is based at Nextiva's Scottsdale headquarters. Are you able to work fully on-site?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "akuna-application",
                "label": "Have you applied to this role at Akuna previously?",
                "options": ["Yes", "No"],
                "value": "",
            },
        ]

        rendered = qa.explicit_approved_answers(
            controls,
            company_context="Nextiva Akuna Capital",
            approved_answers=approved,
        )

        self.assertEqual(
            {
                "high-school-year": "2024",
                "driver": "Yes",
                "agreement": "No",
                "notice": "None",
                "publications": "None",
                "nextiva-employee": "No",
                "nextiva-onsite": "Yes",
                "akuna-application": "No",
            },
            {item["id_or_name"]: item["answer"] for item in rendered},
        )

        right_answers = [
            {"id_or_name": item["id"], "answer": expected}
            for item, expected in zip(
                controls,
                ["2024", "Yes", "No", "None", "None", "No", "Yes", "No"],
            )
        ]
        allowed, blocked = qa.filter_manual_answers(
            [dict(item, company_context="Nextiva Akuna Capital") for item in controls],
            right_answers,
            approved_answers=approved,
        )
        self.assertEqual({item["id"] for item in controls}, {
            item["id_or_name"] for item in allowed
        })
        self.assertEqual([], blocked)

        wrong_controls = [controls[index] for index in (1, 2, 4, 6, 7)]
        wrong_answers = [
            {"id_or_name": item["id"], "answer": answer}
            for item, answer in zip(wrong_controls, ["No", "Yes", "A paper", "No", "Yes"])
        ]
        allowed, blocked = qa.filter_manual_answers(
            [dict(item, company_context="Nextiva Akuna Capital") for item in wrong_controls],
            wrong_answers,
            approved_answers=approved,
        )
        self.assertEqual([], allowed)
        self.assertEqual(
            {item["id"] for item in wrong_controls},
            {item["id_or_name"] for item in blocked},
        )

        unrelated_onsite = {
            "id": "other-onsite",
            "label": "Are you able to work fully on-site?",
            "options": ["Yes", "No"],
            "value": "",
        }
        self.assertEqual([], qa.explicit_approved_answers(
            [unrelated_onsite],
            company_context="Other Company",
            approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            unrelated_onsite,
            "Yes",
            approved_answers=approved,
        ))

    def test_new_confirmed_sections_are_only_exposed_to_relevant_questions(self):
        approved = {
            "version": 1,
            "legal": {"valid_drivers_license": True},
            "professional": {"publications": []},
        }
        driver = qa.relevant_application_answers(
            [{"label": "Do you have a valid driver's license?"}], approved=approved
        )
        publications = qa.relevant_application_answers(
            [{"label": "Please list your publications"}], approved=approved
        )
        unrelated = qa.relevant_application_answers(
            [{"label": "Why are you interested in this role?"}], approved=approved
        )
        self.assertEqual(approved["legal"], driver["legal"])
        self.assertNotIn("professional", driver)
        self.assertEqual(approved["professional"], publications["professional"])
        self.assertNotIn("legal", publications)
        self.assertEqual({"version": 1}, unrelated)

    def test_reviewed_wording_keeps_confirmed_facts_and_polarity_exact(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["education"]["high_school_graduation_year"] = "2024"
        approved["legal"] = {
            "non_compete_or_conflict": False,
            "notice_period": "None",
            "valid_drivers_license": True,
        }
        approved["company_facts"]["Nextiva"] = {
            "fully_onsite": True,
        }
        controls = [
            {
                "id": "high-school-year",
                "label": "What is your high school graduation year?",
                "value": "",
            },
            {
                "id": "notice-boolean",
                "label": "Do you have a non-compete or notice period with your current employer?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "hybrid-required",
                "label": "Do you require a hybrid work schedule?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "hybrid-willing",
                "label": "Are you willing to work a hybrid schedule?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "curly-driver",
                "label": "Do you have a valid driver’s license?",
                "options": ["Yes", "No"],
                "value": "",
            },
        ]
        expected = {
            "high-school-year": "2024",
            "notice-boolean": "No",
            "hybrid-required": "No",
            "hybrid-willing": "Yes",
            "curly-driver": "Yes",
        }

        rendered = qa.explicit_approved_answers(
            controls,
            company_context="Nextiva",
            approved_answers=approved,
        )
        self.assertEqual(expected, {
            item["id_or_name"]: item["answer"] for item in rendered
        })

        policy_controls = [dict(item, company_context="Nextiva") for item in controls]
        allowed, blocked = qa.filter_manual_answers(
            policy_controls,
            [{"id_or_name": key, "answer": value} for key, value in expected.items()],
            approved_answers=approved,
        )
        self.assertEqual(set(expected), {item["id_or_name"] for item in allowed})
        self.assertEqual([], blocked)

        wrong = {
            "high-school-year": "2028",
            "notice-boolean": "Yes",
            "hybrid-required": "Yes",
            "hybrid-willing": "No",
            "curly-driver": "No",
        }
        allowed, blocked = qa.filter_manual_answers(
            policy_controls,
            [{"id_or_name": key, "answer": value} for key, value in wrong.items()],
            approved_answers=approved,
        )
        self.assertEqual([], allowed)
        self.assertEqual(set(wrong), {item["id_or_name"] for item in blocked})

    def test_behavioral_conflict_does_not_expose_legal_facts_or_get_blocked(self):
        approved = {
            "version": 1,
            "legal": {
                "non_compete_or_conflict": False,
                "valid_drivers_license": True,
            },
        }
        control = {
            "id": "story",
            "label": "Describe a conflict you resolved with a teammate.",
            "value": "",
        }
        self.assertEqual(
            {"version": 1},
            qa.relevant_application_answers([control], approved=approved),
        )
        self.assertFalse(qa.answer_requires_manual(
            control,
            "I resolved the disagreement by aligning on shared evidence.",
            approved_answers=approved,
        ))

    def test_get_answers_logs_allowed_and_blocked_with_context_without_api(self):
        controls = [
            {"id": "q1", "name": "", "label": "Why us?", "value": ""},
            {"id": "q2", "name": "", "label": "Desired compensation", "value": ""},
        ]
        model_answers = [
            {"id_or_name": "q1", "answer": "Because the mission fits."},
            {"id_or_name": "q2", "answer": "$50/hour"},
        ]

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_json_load(_resp):
            return {"content": [{"type": "text", "text": json.dumps(model_answers)}]}

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(qa, "ROOT", Path(tmp)), \
             mock.patch.object(qa.urllib.request, "urlopen", return_value=FakeResponse()), \
             mock.patch.object(qa.json, "load", side_effect=fake_json_load), \
             mock.patch.object(qa, "_grounding", return_value=""):
            (Path(tmp) / "out").mkdir()
            answers = qa.get_answers(controls, context={"slug": "acme", "url": "https://example.test/job"})
            rows = [(json.loads(line)) for line in (Path(tmp) / "out" / "qa_answers.log").read_text().splitlines()]

        self.assertEqual([model_answers[0]], answers)
        self.assertEqual(["allowed", "blocked_manual"], [r["decision"] for r in rows])
        self.assertEqual(["Why us?", "Desired compensation"], [r["question"] for r in rows])
        self.assertEqual(["acme", "acme"], [r["slug"] for r in rows])
        self.assertEqual(["https://example.test/job", "https://example.test/job"], [r["url"] for r in rows])


if __name__ == "__main__":
    unittest.main()
