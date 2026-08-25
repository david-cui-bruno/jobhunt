import json
import sys
import tempfile
import unittest

import pytest
import yaml
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))
import qa  # noqa: E402
import ashby  # noqa: E402
import greenhouse  # noqa: E402
import smartrecruiters  # noqa: E402
import workday  # noqa: E402


APPROVED_AUG_25 = {
    "version": 1,
    "legal": {
        "security_clearance": "None",
        "us_dod_employment_after_2008_01_28": False,
        "self_or_family_or_business_partner_government_employment": False,
    },
    "professional": {
        "english_proficiency": "Fluent",
        "publications": [],
    },
    "documents": {
        "unofficial_transcript": {
            "available": True,
            "application_upload_authorized": True,
        },
    },
    "events": {
        "neurips_2026": {"attending": False},
    },
    "availability": {
        "default_start_date": "05/15/2027",
        "summer_2027": {
            "pursue": True,
            "start": "05/15/2027",
            "end": "August 2027",
        },
    },
    "company_facts": {
        "Availity": {
            "household_employment": False,
            "prior_application": False,
        },
        "Akuna Capital": {"prior_application": False},
        "American Fidelity": {"prior_application": False},
        "Point72": {"prior_application": False},
    },
}


def test_august_25_facts_render_exactly_and_wrong_values_remain_manual():
    controls = [
        {"id": "clear", "label": "What level is your US government security clearance?", "options": ["None", "Public Trust", "Secret"]},
        {"id": "dod", "label": "Were you a US Department of Defense employee on or after January 28, 2008?", "options": ["Yes", "No"]},
        {"id": "gov", "label": "Have you or your immediate family or business partners worked for a government entity?", "options": ["Yes", "No"]},
        {"id": "english", "label": "English proficiency", "options": ["Basic", "Conversational", "Fluent", "Native"]},
        {"id": "neurips", "label": "Will you attend NeurIPS 2026?", "options": ["Yes", "No"]},
        {"id": "transcript", "label": "May we upload your unofficial transcript?", "options": ["Yes", "No"]},
        {"id": "start", "label": "When can you start?", "options": []},
    ]
    answers = qa.explicit_approved_answers(controls, approved_answers=APPROVED_AUG_25)
    assert {a["id_or_name"]: a["answer"] for a in answers} == {
        "clear": "None", "dod": "No", "gov": "No", "english": "Fluent",
        "neurips": "No", "transcript": "Yes", "start": "05/15/2027",
    }
    assert qa.answer_requires_manual(controls[0], "Secret", approved_answers=APPROVED_AUG_25)
    assert qa.answer_requires_manual(controls[1], "Yes", approved_answers=APPROVED_AUG_25)
    assert qa.answer_requires_manual(controls[2], "Yes", approved_answers=APPROVED_AUG_25)


def test_official_transcript_does_not_borrow_unofficial_upload_authorization():
    control = {
        "id": "official-transcript",
        "label": "May we upload your official transcript?",
        "options": ["Yes", "No"],
    }

    assert qa.explicit_approved_answers([control], approved_answers=APPROVED_AUG_25) == []
    assert qa.answer_requires_manual(control, "Yes", approved_answers=APPROVED_AUG_25)
    assert qa.answer_requires_manual(control, "No", approved_answers=APPROVED_AUG_25)


@pytest.mark.parametrize(("company", "label"), [
    ("Point72", "Have you previously applied to Point72?"),
    ("Akuna Capital", "Have you applied to this role at Akuna previously?"),
    ("Availity", "Have you previously applied to Availity?"),
    ("American Fidelity", "Have you previously applied to American Fidelity?"),
])
def test_company_scoped_prior_application_renders_no_only_for_intended_company(company, label):
    control = {"id": "prior", "label": label, "options": ["Yes", "No"]}

    rendered = qa.explicit_approved_answers(
        [control], company_context=company, approved_answers=APPROVED_AUG_25,
    )

    assert rendered == [{"id_or_name": "prior", "answer": "No"}]
    assert not qa.answer_requires_manual(
        dict(control, company_context=company), "No", approved_answers=APPROVED_AUG_25,
    )
    assert qa.answer_requires_manual(
        dict(control, company_context=company), "Yes", approved_answers=APPROVED_AUG_25,
    )


def test_company_scoped_prior_application_does_not_leak_to_similarly_named_employer():
    control = {
        "id": "prior",
        "label": "Have you previously applied to American Fidelity National Bank?",
        "options": ["Yes", "No"],
    }

    assert qa.explicit_approved_answers(
        [control], company_context="American Fidelity National Bank", approved_answers=APPROVED_AUG_25,
    ) == []
    assert qa.answer_requires_manual(
        dict(control, company_context="American Fidelity National Bank"),
        "No",
        approved_answers=APPROVED_AUG_25,
    )


def test_clearance_no_clearance_synonyms_require_approved_no_clearance_fact():
    control = {
        "id": "clear",
        "label": "What level is your US government security clearance?",
        "options": ["None", "No clearance", "Secret"],
    }
    missing = json.loads(json.dumps(APPROVED_AUG_25))
    missing["legal"]["security_clearance"] = None
    mismatch = json.loads(json.dumps(APPROVED_AUG_25))
    mismatch["legal"]["security_clearance"] = "Public Trust"

    assert qa.explicit_approved_answers([control], approved_answers=missing) == []
    assert qa.answer_requires_manual(control, "None", approved_answers=missing)
    assert qa.explicit_approved_answers([control], approved_answers=mismatch) == []
    assert qa.answer_requires_manual(control, "None", approved_answers=mismatch)


def test_non_government_employment_wording_stays_manual():
    control = {
        "id": "nongov",
        "label": "Have you or your immediate family worked for a non-government entity?",
        "options": ["Yes", "No"],
    }

    assert qa.explicit_approved_answers([control], approved_answers=APPROVED_AUG_25) == []
    assert qa.answer_requires_manual(control, "No", approved_answers=APPROVED_AUG_25)


def test_empty_company_context_does_not_authorize_question_substring_company_fact():
    control = {
        "id": "prior",
        "label": "Have you previously applied to Point72?",
        "options": ["Yes", "No"],
    }

    assert qa.explicit_approved_answers([control], approved_answers=APPROVED_AUG_25) == []
    assert qa.answer_requires_manual(control, "No", approved_answers=APPROVED_AUG_25)


def test_company_context_accepts_ordinary_legal_suffix_but_not_descriptive_suffix():
    availity_llc = {
        "id": "prior-availity",
        "label": "Have you previously applied here?",
        "options": ["Yes", "No"],
        "company_context": "Availity LLC",
    }
    american_fidelity_bank = {
        "id": "prior-af-bank",
        "label": "Have you previously applied here?",
        "options": ["Yes", "No"],
        "company_context": "American Fidelity National Bank",
    }

    assert qa.explicit_approved_answers(
        [availity_llc], company_context="Availity LLC", approved_answers=APPROVED_AUG_25,
    ) == [{"id_or_name": "prior-availity", "answer": "No"}]
    assert not qa.answer_requires_manual(
        availity_llc, "No", approved_answers=APPROVED_AUG_25,
    )
    assert qa.explicit_approved_answers(
        [american_fidelity_bank], company_context="American Fidelity National Bank", approved_answers=APPROVED_AUG_25,
    ) == []
    assert qa.answer_requires_manual(
        american_fidelity_bank, "No", approved_answers=APPROVED_AUG_25,
    )


def test_internship_start_date_renders_exactly_and_rejects_contradictions():
    control = {
        "id": "internship-start",
        "label": "What is your earliest internship start date?",
        "options": [],
    }

    assert qa.explicit_approved_answers(
        [control], approved_answers=APPROVED_AUG_25,
    ) == [{"id_or_name": "internship-start", "answer": "05/15/2027"}]
    assert not qa.answer_requires_manual(
        control, "05/15/2027", approved_answers=APPROVED_AUG_25,
    )
    assert qa.answer_requires_manual(
        control, "06/01/2027", approved_answers=APPROVED_AUG_25,
    )


@pytest.mark.parametrize(("label", "section", "key"), [
    ("What level is your US government security clearance?", "legal", "security_clearance"),
    (
        "Were you a US Department of Defense employee on or after January 28, 2008?",
        "legal",
        "us_dod_employment_after_2008_01_28",
    ),
    ("What is your English proficiency?", "professional", "english_proficiency"),
    ("May we upload your unofficial transcript?", "documents", "unofficial_transcript"),
    ("Will you attend NeurIPS 2026?", "events", "neurips_2026"),
    ("When can you start?", "availability", "default_start_date"),
])
def test_relevant_application_answers_exposes_only_matching_new_fact(label, section, key):
    result = qa.relevant_application_answers(
        [{"id": "q", "label": label}], APPROVED_AUG_25,
    )

    assert result[section] == {key: APPROVED_AUG_25[section][key]}


def test_new_sensitive_sections_are_not_exposed_to_unrelated_questions():
    result = qa.relevant_application_answers(
        [{"id": "q", "label": "Why do you want this role?"}], APPROVED_AUG_25,
    )

    assert "legal" not in result
    assert "professional" not in result
    assert "documents" not in result
    assert "events" not in result
    assert "availability" not in result


def test_household_and_prior_application_facts_do_not_cross_company_contexts():
    availity = qa.relevant_application_answers(
        [{"id": "q", "label": "Do you have relatives employed by Availity?"}],
        APPROVED_AUG_25,
        company_context="Availity",
    )
    assert set(availity["company_facts"]) == {"Availity"}

    unrelated = qa.relevant_application_answers(
        [{"id": "q", "label": "Do you have relatives employed here?"}],
        APPROVED_AUG_25,
        company_context="Another Company",
    )
    assert "company_facts" not in unrelated


def test_missing_new_structured_values_are_not_exposed():
    approved = {
        "version": 1,
        "documents": {},
        "events": {},
        "availability": {},
        "professional": {},
        "legal": {},
    }
    result = qa.relevant_application_answers(
        [{"id": "q", "label": "Upload your transcript and tell us your English proficiency."}],
        approved,
    )

    assert result == {"version": 1}


def test_application_answers_example_schema_covers_private_runtime_delta():
    # This uses the sanitized schema file directly instead of qa.APPLICATION_ANSWERS,
    # which is intentionally environment-dependent at import time.
    example = yaml.safe_load((qa.ROOT / "profile" / "application_answers.example.yaml").read_text())

    assert example["availability"]["default_start_date"] is None
    assert example["availability"]["summer_2027"] == {
        "pursue": True,
        "start": None,
        "end": None,
    }
    assert example["current_offers"] == [{
        "company": None,
        "description": None,
        "deadline_month": None,
        "deadline": None,
        "deadline_note": None,
    }]
    assert example["legal"]["security_clearance"] is None
    assert example["legal"]["us_dod_employment_after_2008_01_28"] is None
    assert example["legal"]["self_or_family_or_business_partner_government_employment"] is None
    assert example["professional"]["english_proficiency"] is None
    assert example["documents"]["unofficial_transcript"] == {
        "available": None,
        "application_upload_authorized": None,
    }
    assert example["events"]["example_event"]["attending"] is None


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
            "relocate": True,
            "relocation_assistance_required": False,
            "compensation_policy": "Use an employer-published range; otherwise open / market rate.",
        },
        "education": {
            "expected_graduation_month": "June",
            "expected_graduation_year": "2028",
            "exact_graduation_date": "06/01/2028",
            "standardized_tests": {
                "sat": 1570,
                "act": None,
                "act_taken": False,
            },
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

        # This test exercises the fail-closed baseline, independent of any
        # operator-approved answer bank loaded by the production environment.
        allowed, blocked = qa.filter_manual_answers(
            controls, answers, profile_text="", approved_answers={},
        )

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

    def test_ready_for_fulltime_in_year_uses_graduation_fact(self):
        control = {
            "id": "ready",
            "label": "Will you be ready for full-time employment in 2028?*",
            "options": ["Yes", "No"],
            "value": "",
        }
        self.assertFalse(qa.answer_requires_manual(
            control, "Yes", approved_answers=self.APPROVED,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "No", approved_answers=self.APPROVED,
        ))
        earlier = dict(control, label="Will you be ready for full-time employment in 2027?*")
        self.assertTrue(qa.answer_requires_manual(
            earlier, "Yes", approved_answers=self.APPROVED,
        ))
        self.assertFalse(qa.answer_requires_manual(
            earlier, "No", approved_answers=self.APPROVED,
        ))

    def test_fulltime_permanent_availability_derives_from_graduation(self):
        control = {
            "id": "avail",
            "label": ("When will you be available to work as a full-time, permanent "
                      "employee? Full-time means working 40 hours per week while "
                      "being based in our San Mateo, CA headquarters"),
            "options": ["Available to Start Immediately", "Fall 2027", "Winter 2027",
                        "Spring 2028", "Summer 2028", "Fall 2028"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers(
            [control], approved_answers=self.APPROVED,
        )
        self.assertEqual(
            [{"id_or_name": "avail", "answer": "Summer 2028"}], rendered,
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "Summer 2028", approved_answers=self.APPROVED,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Available to Start Immediately", approved_answers=self.APPROVED,
        ))

    def test_first_hear_about_role_uses_approved_sources(self):
        approved = dict(self.APPROVED)
        approved["preferences"] = dict(
            self.APPROVED["preferences"], recruiting_sources=["LinkedIn", "Handshake"],
        )
        control = {
            "id": "hear",
            "label": "How did you first hear about this role?*",
            "options": ["Roblox Careers Site", "Campus Ambassador", "LinkedIn",
                        "Handshake", "Word of Mouth", "Other"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers(
            [control], approved_answers=approved,
        )
        self.assertEqual(
            [{"id_or_name": "hear", "answer": "LinkedIn"}], rendered,
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "LinkedIn", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Referred by Roblox Employee", approved_answers=approved,
        ))

    def test_ethnicity_declines_via_undisclosed_label(self):
        """G-Research 2026-08-17: tenant's decline label is 'Undisclosed' and
        its posting text instructs non-consenting applicants to select it."""
        control = {
            "id": "eth",
            "label": "What is your ethnicity?*",
            "options": ["Asian", "Black", "Hispanic or Latino", "Mixed",
                        "Undisclosed", "White"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers(
            [control], approved_answers=self.APPROVED,
        )
        self.assertEqual(
            [{"id_or_name": "eth", "answer": "Undisclosed"}], rendered,
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "Undisclosed", approved_answers=self.APPROVED,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Asian", approved_answers=self.APPROVED,
        ))

    def test_prior_employment_answers_no_via_employment_history(self):
        """User statement 2026-08-17: only ever employed at Framewise/Freya/
        Sotatek. Any other company's 'worked here before' is truthfully No."""
        approved = dict(self.APPROVED)
        approved["employment_history"] = {
            "only_employers_ever": ["Framewise Health", "Freya", "Sotatek"],
        }
        control = {
            "id": "prior",
            "label": "Have you previously worked for McKesson?*",
            "options": ["Yes", "No"],
            "value": "",
            "company_context": "mckesson",
        }
        rendered = qa.explicit_approved_answers(
            [control], approved_answers=approved,
        )
        self.assertEqual([{"id_or_name": "prior", "answer": "No"}], rendered)
        self.assertFalse(qa.answer_requires_manual(
            control, "No", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Yes", approved_answers=approved,
        ))
        # A listed employer keeps failing closed without an explicit fact.
        freya = {
            "id": "prior2",
            "label": "Have you previously worked for Freya?*",
            "options": ["Yes", "No"],
            "value": "",
            "company_context": "freya",
        }
        self.assertEqual([], qa.explicit_approved_answers(
            [freya], approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            freya, "No", approved_answers=approved,
        ))
        # Without the history fact the fallback stays closed.
        self.assertEqual([], qa.explicit_approved_answers(
            [control], approved_answers=self.APPROVED,
        ))

    def test_binary_hispanic_question_derives_from_race_fact(self):
        approved = dict(self.APPROVED)
        approved["identity"] = dict(self.APPROVED["identity"], race_ethnicity="Asian")
        control = {
            "id": "hisp",
            "label": "Are you Hispanic/Latino?",
            "options": ["Yes", "No", "Decline To Self Identify"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers(
            [control], approved_answers=approved,
        )
        self.assertEqual([{"id_or_name": "hisp", "answer": "No"}], rendered)
        self.assertFalse(qa.answer_requires_manual(
            control, "No", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Yes", approved_answers=approved,
        ))
        # Race dropdowns still render the fact itself.
        race_control = {
            "id": "race",
            "label": "Race/Ethnicity*",
            "options": ["Asian", "White", "Black or African American",
                        "I don't wish to answer"],
            "value": "",
        }
        self.assertEqual(
            [{"id_or_name": "race", "answer": "Asian"}],
            qa.explicit_approved_answers([race_control], approved_answers=approved),
        )
        self.assertFalse(qa.answer_requires_manual(
            race_control, "Asian", approved_answers=approved,
        ))

    def test_ethnicity_accepts_workday_decorated_labels(self):
        """CCC/Motorola/DataRobot 2026-08-17: Workday EEO menus decorate the
        approved base label with parenthetical qualifiers."""
        approved = dict(self.APPROVED)
        approved["identity"] = dict(self.APPROVED["identity"], race_ethnicity="Asian")
        control = {
            "id": "eth",
            "label": "Please select the ethnicity which most accurately describes how you identify yourself.*",
            "options": ["American Indian (United States of America)",
                        "Asian (United States of America)",
                        "Black or African American (United States of America)",
                        "White (United States of America)"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers([control], approved_answers=approved)
        self.assertEqual(
            [{"id_or_name": "eth", "answer": "Asian (United States of America)"}],
            rendered,
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "Asian (United States of America)", approved_answers=approved,
        ))
        self.assertFalse(qa.answer_requires_manual(
            control, "Asian (Not Hispanic or Latino) (United States of America)",
            approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "White (United States of America)", approved_answers=approved,
        ))
        # 'Caucasian/Asian ancestry (mixed)' style tricks must not pass.
        self.assertTrue(qa.answer_requires_manual(
            control, "South Asian (United States of America)", approved_answers=approved,
        ))

    def test_filter_manual_answers_survives_malformed_entries(self):
        """Luminance 2026-08-17: a model emitted a nested list inside the
        answers array and filter_manual_answers crashed on .get()."""
        control = {"id": "q1", "label": "Preferred First Name", "options": [], "value": ""}
        answers, blocked = qa.filter_manual_answers(
            [control],
            [["nested", "list"], "stray string", None,
             {"id_or_name": "q1", "answer": "David"}],
        )
        self.assertEqual([{"id_or_name": "q1", "answer": "David", "label": "Preferred First Name"}], answers)
        self.assertEqual([], blocked)

    def test_hours_per_week_quantity_only_approves_forty(self):
        """NLR 2026-08-17: 'How many hours per week can you work?' was
        approved with a literal 'Yes' via the hybrid-preference fallback."""
        control = {"id": "q", "label": "How many hours per week can you work?*",
                   "options": [], "value": ""}
        self.assertTrue(qa.answer_requires_manual(
            control, "Yes", approved_answers=self.APPROVED))
        self.assertTrue(qa.answer_requires_manual(
            control, "20", approved_answers=self.APPROVED))
        self.assertFalse(qa.answer_requires_manual(
            control, "40", approved_answers=self.APPROVED))
        self.assertFalse(qa.answer_requires_manual(
            control, "40 hours per week", approved_answers=self.APPROVED))

    def test_proficiency_checklist_is_not_a_product_usage_question(self):
        """Medtronic 2026-08-17: 'proficient in the following software
        languages' tripped the used-our-product block pattern."""
        control = {
            "id": "m",
            "label": ("Are you proficient in the following software languages? "
                      "Please select all that apply*"),
            "options": ["Python", "C/C++", "SQL", "JavaScript", "None"],
            "value": "",
        }
        self.assertFalse(qa.answer_requires_manual(
            control, ["Python", "SQL"], approved_answers=self.APPROVED))
        used_product = {"id": "u", "label": "Have you used our software platform before?",
                        "options": ["Yes", "No"], "value": ""}
        self.assertTrue(qa.answer_requires_manual(
            used_product, "Yes", approved_answers=self.APPROVED))

    def test_vevraa_veteran_paragraph_declines(self):
        """Cadence 2026-08-17: the VEVRAA veteran dropdown label never says
        'veteran status', and 'disabled veterans' in the paragraph routed the
        answer into the disability branch, blocking the whole page."""
        control = {
            "id": "vet",
            "label": ("This employer is a Government contractor subject to the Vietnam "
                      "Era Veterans' Readjustment Assistance Act of 1974, as amended by "
                      "the Jobs for Veterans Act of 2002, 38 U.S.C. 4212 (VEVRAA), which "
                      "requires Government contractors to take affirmative action to "
                      "employ and advance in employment protected veterans... regarding "
                      "disabled veterans and reasonable accommodations...*"),
            "options": ["I IDENTIFY AS ONE OR MORE OF THE CLASSIFICATIONS OF PROTECTED "
                        "VETERAN LISTED ABOVE",
                        "I AM NOT A VETERAN", "I DO NOT WISH TO ANSWER"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers([control], approved_answers=self.APPROVED)
        self.assertEqual(
            [{"id_or_name": "vet", "answer": "I DO NOT WISH TO ANSWER"}], rendered,
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "I DO NOT WISH TO ANSWER", approved_answers=self.APPROVED))
        self.assertTrue(qa.answer_requires_manual(
            control, "I IDENTIFY AS ONE OR MORE OF THE CLASSIFICATIONS OF PROTECTED "
                     "VETERAN LISTED ABOVE", approved_answers=self.APPROVED))

    def test_vevraa_truncated_label_uses_not_a_veteran_fact(self):
        """Cadence 2026-08-17: WD_EXTRACT_JS truncates the paragraph at 250
        chars, ending in 'take affirmative act', which the ACT-score pattern
        swallowed before the veteran branch; and with veteran=False the honest
        'I AM NOT A VETERAN' option parses as no boolean, so rendering fell
        through to the decline label (absent from this tenant's menu)."""
        approved = {**self.APPROVED,
                    "identity": {**self.APPROVED["identity"], "veteran": False}}
        control = {
            "id": "vet",
            "label": ("This employer is a Government contractor subject to the Vietnam "
                      "Era Veterans' Readjustment Assistance Act of 1974, as amended by "
                      "the Jobs for Veterans Act of 2002, 38 U.S.C. 4212 (VEVRAA), which "
                      "requires Government contractors to take affirmative act"),
            "options": ["I IDENTIFY AS ONE OR MORE OF THE CLASSIFICATIONS OF PROTECTED "
                        "VETERAN LISTED ABOVE",
                        "I AM NOT A VETERAN", "I DO NOT WISH TO SELF-IDENTIFY"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers([control], approved_answers=approved)
        self.assertEqual(
            [{"id_or_name": "vet", "answer": "I AM NOT A VETERAN"}], rendered)
        self.assertFalse(qa.answer_requires_manual(
            control, "I AM NOT A VETERAN", approved_answers=approved))
        self.assertFalse(qa.answer_requires_manual(
            control, "I DO NOT WISH TO SELF-IDENTIFY", approved_answers=approved))

    def test_ai_screening_truncated_label_detects_opt_out_from_options(self):
        """Crowe 2026-08-17: the 250-char label cut ends before the paragraph's
        'opt out' sentence, so the notice was only recognizable from its
        Opt In / Opt Out menu options."""
        control = {
            "id": "jit",
            "label": ("Just-In-Time Notice How we review applications & your choices: "
                      "We use an AI\u2011assisted resume\u2011screening tool to help "
                      "recruiters manage applications. Based on the job posting, the "
                      "tool reads only the information you provide and may ide"),
            "options": ["Opt In", "Opt Out"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers([control], approved_answers=self.APPROVED)
        self.assertEqual([{"id_or_name": "jit", "answer": "Opt In"}], rendered)
        self.assertFalse(qa.answer_requires_manual(
            control, "Opt In", approved_answers=self.APPROVED))
        self.assertTrue(qa.answer_requires_manual(
            control, "Opt Out", approved_answers=self.APPROVED))

    def test_age_18_or_over_wording(self):
        """Nelnet 2026-08-17: 'Are you age 18 or over?' says 'over', not
        'older', so the derived age answer was never rendered or approved."""
        control = {"id": "a", "label": "Are you age 18 or over?*",
                   "options": ["Yes", "No"], "value": ""}
        rendered = qa.explicit_approved_answers([control], approved_answers=self.APPROVED)
        self.assertEqual([{"id_or_name": "a", "answer": "Yes"}], rendered)
        self.assertFalse(qa.answer_requires_manual(
            control, "Yes", approved_answers=self.APPROVED))

    def test_sat_act_best_result_wording(self):
        """IMC 2026-08-17: 'Provide your best result on SAT/ACT' contains no
        score/test keyword, so the model invented 'I don't have SAT score'."""
        sat = {"id": "s", "label": "Provide your best result on SAT:*", "value": ""}
        act = {"id": "a", "label": "Provide your best result on ACT:*", "value": ""}
        rendered = qa.explicit_approved_answers([sat, act], approved_answers=self.APPROVED)
        self.assertEqual([
            {"id_or_name": "s", "answer": "1570"},
            {"id_or_name": "a", "answer": "Did not take"},
        ], rendered)
        self.assertTrue(qa.answer_requires_manual(
            sat, "I don't have SAT score", approved_answers=self.APPROVED))
        self.assertFalse(qa.answer_requires_manual(
            sat, "1570", approved_answers=self.APPROVED))

    def test_highest_completed_education_falls_back_to_hs_diploma(self):
        """Belvedere 2026-08-17: the menu lists only completed credentials, so
        the truthful highest COMPLETED level for a current undergrad is the
        high school diploma."""
        approved = {**self.APPROVED,
                    "education": {**self.APPROVED["education"],
                                  "high_school_graduation_year": "2024"}}
        control = {
            "id": "e",
            "label": ("Please select the highest level of education that you have "
                      "completed or will complete prior to the start of this "
                      "position.*"),
            "options": ["High School Diploma", "Associate Degree",
                        "Bachelor Degree", "Masters/PhD"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers([control], approved_answers=approved)
        self.assertEqual(
            [{"id_or_name": "e", "answer": "High School Diploma"}], rendered)

    def test_sat_range_and_act_no_score_menus(self):
        """IMC 2026-08-17 retry: the SAT menu offers ranges ('1501 - 1600')
        and the ACT menu offers "I don't have ACT score"; exact-option
        matching alone left both manual."""
        sat = {"id": "s", "label": "Provide your best result on SAT:*",
               "options": ["1201 - 1300", "1301 - 1400", "1401 - 1500",
                           "1501 - 1600", "I don't have SAT score"],
               "value": ""}
        act = {"id": "a", "label": "Provide your best result on ACT:*",
               "options": ["30 - 33", "34 - 36", "I don't have ACT score"],
               "value": ""}
        rendered = qa.explicit_approved_answers([sat, act], approved_answers=self.APPROVED)
        self.assertEqual([
            {"id_or_name": "s", "answer": "1501 - 1600"},
            {"id_or_name": "a", "answer": "I don't have ACT score"},
        ], rendered)
        self.assertFalse(qa.answer_requires_manual(
            sat, "1501 - 1600", approved_answers=self.APPROVED))
        self.assertFalse(qa.answer_requires_manual(
            act, "I don't have ACT score", approved_answers=self.APPROVED))
        self.assertTrue(qa.answer_requires_manual(
            sat, "1401 - 1500", approved_answers=self.APPROVED))

    def test_pursuing_degree_and_expect_to_graduate_menus(self):
        """Belvedere 2026-08-17: Lever radio groups asked 'What degree are you
        currently pursuing?' and 'When do you expect to graduate?' with
        season-range options; neither matched the graduation-date patterns."""
        deg = {"id": "d", "label": "What degree are you currently pursuing?",
               "options": ["High School Diploma", "Associate Degree",
                           "Bachelor Degree", "Masters/PhD"], "value": ""}
        grad = {"id": "g", "label": "When do you expect to graduate?",
                "options": ["December 2026/January 2027", "Spring 2027",
                            "December 2027/January 2028", "Spring 2028",
                            "Other"], "value": ""}
        rendered = qa.explicit_approved_answers([deg, grad], approved_answers=self.APPROVED)
        self.assertEqual([
            {"id_or_name": "d", "answer": "Bachelor Degree"},
            {"id_or_name": "g", "answer": "Spring 2028"},
        ], rendered)
        self.assertFalse(qa.answer_requires_manual(
            deg, "Bachelor Degree", approved_answers=self.APPROVED))
        self.assertFalse(qa.answer_requires_manual(
            grad, "Spring 2028", approved_answers=self.APPROVED))
        self.assertTrue(qa.answer_requires_manual(
            grad, "Spring 2027", approved_answers=self.APPROVED))

    def test_controls_json_keeps_trailing_controls(self):
        """Belvedere 2026-08-17: a 3000-option school select pushed the five
        card fields after it past the flat 20000-char slice, so the model
        never saw them. Option lists shrink; controls never drop."""
        controls = ([{"id": "school", "label": "Name of School",
                      "options": [f"School {i}" for i in range(3000)], "value": ""}]
                    + [{"id": f"q{i}", "label": f"Question {i}", "options": [],
                        "value": ""} for i in range(5)])
        text = qa._controls_json(controls)
        self.assertLessEqual(len(text), 20000)
        parsed = json.loads(text)
        self.assertEqual(6, len(parsed))
        self.assertEqual("q4", parsed[-1]["id"])
        self.assertIn("more options omitted", parsed[0]["options"][-1])

    def test_ai_screening_notice_consents_to_standard_process(self):
        """Crowe 2026-08-17: required Just-In-Time Notice dropdown (consent vs
        Opt Out for AI-assisted resume screening) tripped the used-our-product
        pattern and blocked the run."""
        control = {
            "id": "jit",
            "label": ("Just-In-Time Notice: We use an AI-assisted resume-screening "
                      "tool to help recruiters manage applications... You may opt out "
                      "of automated screening and request manual review at any time by "
                      "contacting us...*"),
            "options": ["I acknowledge and consent to the use of the AI-assisted "
                        "screening tool", "Opt Out"],
            "value": "",
        }
        rendered = qa.explicit_approved_answers([control], approved_answers=self.APPROVED)
        self.assertEqual(
            [{"id_or_name": "jit",
              "answer": "I acknowledge and consent to the use of the AI-assisted "
                        "screening tool"}],
            rendered,
        )
        self.assertFalse(qa.answer_requires_manual(
            control,
            "I acknowledge and consent to the use of the AI-assisted screening tool",
            approved_answers=self.APPROVED))

    def test_combined_gender_labels_match_when_all_segments_agree(self):
        """PSP 2026-08-17: menu bundles 'Man / Trans Man' as one option."""
        control = {
            "id": "g",
            "label": "To which gender identity do you identify?*",
            "options": ["Woman / Trans Woman", "Man / Trans Man", "Non-Binary",
                        "Prefer not to say"],
            "value": "",
        }
        self.assertFalse(qa.answer_requires_manual(
            control, "Man / Trans Man", approved_answers=self.APPROVED))
        self.assertTrue(qa.answer_requires_manual(
            control, "Woman / Trans Woman", approved_answers=self.APPROVED))
        rendered = qa.explicit_approved_answers([control], approved_answers=self.APPROVED)
        self.assertEqual(
            [{"id_or_name": "g", "answer": "Man / Trans Man"}], rendered,
        )

    def test_hours_per_week_renders_forty(self):
        """NLR 2026-08-17: quantity question now renders the approved 40."""
        control = {"id": "h", "label": "How many hours per week can you work?*",
                   "options": [], "value": ""}
        rendered = qa.explicit_approved_answers([control], approved_answers=self.APPROVED)
        self.assertEqual([{"id_or_name": "h", "answer": "40"}], rendered)
        with_options = dict(control, options=["20", "30", "40"])
        rendered = qa.explicit_approved_answers([with_options], approved_answers=self.APPROVED)
        self.assertEqual([{"id_or_name": "h", "answer": "40"}], rendered)

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

    def test_dynamic_workday_recruiting_source_uses_approved_default(self):
        rendered = qa.explicit_approved_answers(
            [{
                "faid": "source",
                "label": "How Did You Hear About Us?*",
                "kind": "multi",
                "options": [],
                "value": "",
            }],
            approved_answers=self.APPROVED,
        )
        self.assertEqual(
            [{"id_or_name": "How Did You Hear About Us?*", "answer": "Company website"}],
            rendered,
        )

    def test_company_recruiting_source_overrides_default_and_rejects_conflict(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["company_facts"]["Valeo"] = {
            "recruiting_source": "Employee Referral",
        }
        control = {
            "id": "source",
            "label": "How Did You Hear About Us?*",
            "kind": "multi",
            "options": ["Company Website", "Employee Referral"],
            "value": "",
            "company_context": "Valeo",
        }

        self.assertEqual(
            [{"id_or_name": "source", "answer": "Employee Referral"}],
            qa.explicit_approved_answers(
                [control], company_context="Valeo", approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "Employee Referral", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Company Website", approved_answers=approved,
        ))

    def test_global_generic_recruiting_sources_choose_an_observed_option(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["preferences"]["recruiting_sources"] = [
            "Social media", "Google", "LinkedIn", "Indeed", "Handshake",
            "Searching for jobs online", "Other Job Board",
        ]
        freeform = {
            "id": "source",
            "label": "How did you hear about us?*",
            "options": [
                "Searching for jobs online",
                "LinkedIn post",
                "Social media (Instagram, Facebook, X)",
            ],
        }
        dynamic = {
            "id": "dynamic",
            "label": "How Did You Hear About Us?*",
            "options": [],
        }
        stepstone = {
            "id": "stepstone-source",
            "label": "How did you hear about this position?*",
            "options": [
                "StepStone Career Board", "Referred", "LinkedIn",
                "Direct Outreach (LinkedIn Inmail)", "Indeed", "Handshake",
                "Other Job Board", "On-campus Event", "Other",
            ],
        }

        self.assertEqual(
            {
                "source": "Social media (Instagram, Facebook, X)",
                "dynamic": "Social media",
                "stepstone-source": "LinkedIn",
            },
            {
                item["id_or_name"]: item["answer"]
                for item in qa.explicit_approved_answers(
                    [freeform, dynamic, stepstone], approved_answers=approved,
                )
            },
        )
        self.assertFalse(qa.answer_requires_manual(
            freeform,
            "Social media (Instagram, Facebook, X)",
            approved_answers=approved,
        ))
        self.assertFalse(qa.answer_requires_manual(
            stepstone, "LinkedIn", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            freeform, "LinkedIn post", approved_answers=approved,
        ))
        relevant = qa.relevant_application_answers(
            [freeform], approved=approved,
        )
        self.assertEqual(
            approved["preferences"]["recruiting_sources"],
            relevant["preferences"]["recruiting_sources"],
        )

        approved["company_facts"]["Valeo"] = {
            "recruiting_source": "Company Website",
        }
        valeo = dict(
            freeform,
            company_context="Valeo",
            options=["Company Website", "Social media"],
        )
        self.assertEqual(
            [{"id_or_name": "source", "answer": "Company Website"}],
            qa.explicit_approved_answers(
                [valeo], company_context="Valeo", approved_answers=approved,
            ),
        )

        for invalid in ([], "Social media"):
            malformed = json.loads(json.dumps(self.APPROVED))
            malformed["preferences"]["recruiting_sources"] = invalid
            self.assertEqual([], qa.explicit_approved_answers(
                [freeform], approved_answers=malformed,
            ))
            self.assertTrue(qa.answer_requires_manual(
                freeform,
                "Social media (Instagram, Facebook, X)",
                approved_answers=malformed,
            ))

    def test_political_contribution_threshold_is_exact_and_fail_closed(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["legal"] = {
            "political_contributions_over_150_last_two_years": False,
        }
        exact = {
            "id": "political",
            "label": (
                "Have you made any political contributions greater than $150 "
                "in the last 2 years?"
            ),
            "options": ["Yes", "No"],
        }
        generic = {
            "id": "generic-political",
            "label": "Have you ever made a political contribution?",
            "options": ["Yes", "No"],
        }
        wrong_amounts = [
            {
                "id": f"political-{amount}",
                "label": (
                    f"Have you made any political contributions greater than {amount} "
                    "in the last 2 years?"
                ),
                "options": ["Yes", "No"],
            }
            for amount in ("$1,150", "$150,000", "$2,150", "$150.50")
        ]

        self.assertEqual(
            [{"id_or_name": "political", "answer": "No"}],
            qa.explicit_approved_answers(
                [exact, generic, *wrong_amounts], approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            exact, "No", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            exact, "Yes", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            generic, "No", approved_answers=approved,
        ))
        for wrong_amount in wrong_amounts:
            self.assertTrue(qa.answer_requires_manual(
                wrong_amount, "No", approved_answers=approved,
            ))
        self.assertEqual(
            approved["legal"],
            qa.relevant_application_answers(
                [exact], approved=approved,
            )["legal"],
        )

    def test_oligo_interest_answer_is_exact_and_company_scoped(self):
        approved = json.loads(json.dumps(self.APPROVED))
        answer = (
            "Oligo stood out because Zenith connects agentic AI and embedded "
            "simulation directly to spacecraft design and manufacturing, so model "
            "outputs have to survive real engineering and hardware constraints rather "
            "than remain demos. That matches how I like to build: I’ve developed "
            "eval-driven multi-agent systems, trained PyTorch models with rigorous "
            "leakage and claim checks, and built a fault-injected C vehicle network. "
            "I’d be excited to apply that systems mindset to requirements reasoning "
            "and simulation-aware ML on hardware that flies."
        )
        approved["long_form_answers"].append({
            "key": "oligo_specific_interest",
            "company": "Oligo Space",
            "company_aliases": ["oligo"],
            "match_all": ["what stood out", "specific position"],
            "answer": answer,
        })
        prompt = {
            "id": "oligo-interest",
            "label": (
                "What stood out about Oligo Space that led you to apply to this "
                "specific position?"
            ),
        }

        self.assertEqual(
            [{"id_or_name": "oligo-interest", "answer": answer}],
            qa.explicit_approved_answers(
                [prompt], company_context="Oligo Space", approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            dict(prompt, company_context="Oligo Space"),
            answer,
            approved_answers=approved,
        ))
        self.assertEqual(
            [{"id_or_name": "oligo-interest", "answer": answer}],
            qa.explicit_approved_answers(
                [prompt], company_context="oligo", approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            dict(prompt, company_context="oligo"),
            answer,
            approved_answers=approved,
        ))
        self.assertEqual([], qa.explicit_approved_answers(
            [prompt], company_context="Other Company", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            dict(prompt, company_context="Other Company"),
            answer,
            approved_answers=approved,
        ))
        generic_elsewhere = {
            "id": "elsewhere",
            "label": "What stood out and led you to apply to this specific position?",
        }
        self.assertEqual([], qa.explicit_approved_answers(
            [generic_elsewhere],
            company_context="Other Company",
            approved_answers=approved,
        ))
        self.assertNotIn(
            "long_form_answers",
            qa.relevant_application_answers(
                [generic_elsewhere],
                approved=approved,
                company_context="Other Company",
            ),
        )

    def test_oligo_intro_is_grounded_exact_and_company_scoped(self):
        approved = json.loads(json.dumps(self.APPROVED))
        answer = (
            "I’m David, a Brown University Computer Science and Economics student "
            "graduating in June 2028 who likes building AI systems that have to work "
            "reliably in the real world. As co-founder and CTO of Framewise Health, I "
            "built a Temporal/Python/Supabase pipeline and React Native/Next.js "
            "products; at Freya, I worked on sub-300 ms LLM voice agents supporting "
            "200+ concurrent calls; and at Sotatek, I built fraud-detection ML and ETL "
            "pipelines processing 10,000+ transactions a day. I’m most energized by "
            "the intersection of ML, distributed systems, and high-consequence "
            "engineering, which is why Oligo’s simulation-aware AI for spacecraft "
            "design is especially compelling to me."
        )
        approved["long_form_answers"].append({
            "key": "oligo_candidate_intro",
            "company": "Oligo Space",
            "company_aliases": ["oligo"],
            "match_all": ["tell us about yourself"],
            "answer": answer,
        })
        prompt = {"id": "intro", "label": "Tell us about yourself!"}

        self.assertEqual(
            [{"id_or_name": "intro", "answer": answer}],
            qa.explicit_approved_answers(
                [prompt], company_context="oligo", approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            dict(prompt, company_context="Oligo Space"),
            answer,
            approved_answers=approved,
        ))
        self.assertEqual([], qa.explicit_approved_answers(
            [prompt], company_context="Other Company", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            dict(prompt, company_context="Other Company"),
            answer,
            approved_answers=approved,
        ))

    def test_replit_project_answers_are_exact_grounded_and_company_scoped(self):
        approved = json.loads(json.dumps(self.APPROVED))
        description = (
            "Framewise Health is a YC-backed healthcare startup I co-founded and "
            "led technically. It turns medical records into clinician-reviewed "
            "patient education videos. I built the production pipeline with Temporal, "
            "Python, Supabase, and Claude, plus React Native and Next.js apps. "
            "Patient-data systems remain private for security."
        )
        interest = (
            "Replit's mission to make software creation accessible resonates with "
            "me. At Framewise Health and Freya, I built production workflows and "
            "real-time LLM systems, making me care about reliable agents and "
            "interfaces that turn intent into working software. I would bring that "
            "builder perspective to Replit."
        )
        entries = [
            {
                "key": "replit_project_url",
                "company": "Replit",
                "match_all": ["project url"],
                "answer": "https://www.framewisehealth.com/",
            },
            {
                "key": "replit_project_password",
                "company": "Replit",
                "match_all": ["project password"],
                "answer": "N/A (public website; no password required)",
            },
            {
                "key": "replit_project_description",
                "company": "Replit",
                "match_all": ["tell us about your submitted project"],
                "answer": description,
            },
            {
                "key": "replit_specific_interest",
                "company": "Replit",
                "match_all": ["why are you interested in replit"],
                "answer": interest,
            },
        ]
        approved["long_form_answers"].extend(entries)
        controls = [
            {"id": "url", "label": "Project URL"},
            {"id": "password", "label": "Project Password"},
            {"id": "description", "label": "Please tell us about your submitted project"},
            {"id": "interest", "label": "Why are you interested in Replit?"},
        ]

        self.assertEqual(
            [
                {"id_or_name": "url", "answer": entries[0]["answer"]},
                {"id_or_name": "password", "answer": entries[1]["answer"]},
                {"id_or_name": "description", "answer": description},
                {"id_or_name": "interest", "answer": interest},
            ],
            qa.explicit_approved_answers(
                controls, company_context="replit", approved_answers=approved,
            ),
        )
        self.assertEqual([], qa.explicit_approved_answers(
            controls, company_context="Other Company", approved_answers=approved,
        ))
        for control, entry in zip(controls, entries):
            self.assertFalse(qa.answer_requires_manual(
                dict(control, company_context="Replit"),
                entry["answer"],
                approved_answers=approved,
            ))
            self.assertTrue(qa.answer_requires_manual(
                dict(control, company_context="Other Company"),
                entry["answer"],
                approved_answers=approved,
            ))

    def test_ashby_company_context_is_stable_across_application_urls(self):
        self.assertEqual(
            "oligo",
            ashby._ashby_company_context(
                "https://jobs.ashbyhq.com/oligo/107f5148/application?embed=true"
            ),
        )
        self.assertEqual("", ashby._ashby_company_context(
            "https://example.com/oligo/107f5148/application"
        ))

    def test_company_employment_type_is_exact_and_does_not_cross_companies(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["company_facts"]["TransMarket Group"] = {
            "employment_type": "Full-time",
        }
        control = {
            "id": "employment-type",
            "label": "What employment type are you seeking?",
            "kind": "multi",
            "options": ["Full-time", "Part-time"],
            "value": "",
            "company_context": "TransMarket Group",
        }

        self.assertEqual(
            [{"id_or_name": "employment-type", "answer": "Full-time"}],
            qa.explicit_approved_answers(
                [control], company_context="TransMarket Group",
                approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "Full-time", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Part-time", approved_answers=approved,
        ))

        other = dict(control, company_context="Other Company")
        self.assertEqual([], qa.explicit_approved_answers(
            [other], company_context="Other Company", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            other, "Full-time", approved_answers=approved,
        ))

    def test_company_hometown_is_exact_and_does_not_fill_phone_country(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["company_facts"]["TransMarket Group"] = {
            "hometown": {
                "city": "Prosper",
                "region": "Texas",
                "country": "United States",
            },
        }
        controls = [
            {
                "id": "question_12685141007",
                "label": "Where is your hometown?*",
                "company_context": "transmarketgroup",
                "options": [],
            },
            {
                "id": "question_12685142007",
                "label": "State/Province/Region:*",
                "company_context": "transmarketgroup",
                "options": [],
            },
            {
                "id": "question_12685143007",
                "label": "Country:*",
                "company_context": "transmarketgroup",
                "options": ["Canada", "United States"],
            },
            {
                "id": "country",
                "label": "Country*",
                "company_context": "transmarketgroup",
                "options": ["Canada", "United States"],
            },
        ]

        self.assertEqual(
            {
                "question_12685141007": "Prosper",
                "question_12685142007": "Texas",
                "question_12685143007": "United States",
            },
            {
                item["id_or_name"]: item["answer"]
                for item in qa.explicit_approved_answers(
                    controls,
                    company_context="transmarketgroup",
                    approved_answers=approved,
                )
            },
        )
        for control, right, wrong in zip(
            controls[:3],
            ("Prosper", "Texas", "USA"),
            ("Providence", "Rhode Island", "Canada"),
        ):
            self.assertFalse(qa.answer_requires_manual(
                control, right, approved_answers=approved,
            ))
            self.assertTrue(qa.answer_requires_manual(
                control, wrong, approved_answers=approved,
            ))

        other_company = [dict(control, company_context="Other Company")
                         for control in controls[:3]]
        self.assertEqual([], qa.explicit_approved_answers(
            other_company,
            company_context="Other Company",
            approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            other_company[0], "Prosper", approved_answers=approved,
        ))

    def test_valeo_prior_employment_is_exact_and_company_scoped(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["company_facts"]["Valeo"] = {"prior_employment": False}
        control = {
            "faid": "prior-employment",
            "label": "Have you previously worked for Valeo?*",
            "options": ["Yes", "No"],
            "company_context": "valeo",
        }
        self.assertEqual(
            [{"faid": "prior-employment", "answer": "No"}],
            qa.explicit_approved_answers(
                [control], key_field="faid", company_context="valeo",
                approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            control, "No", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            control, "Yes", approved_answers=approved,
        ))
        other_control = dict(
            control,
            label="Have you previously worked for us?*",
            company_context="Other Company",
        )
        self.assertEqual([], qa.explicit_approved_answers(
            [other_control],
            key_field="faid", company_context="Other Company",
            approved_answers=approved,
        ))

    def test_greenhouse_company_context_uses_exact_board_token(self):
        self.assertEqual(
            "transmarketgroup",
            greenhouse._greenhouse_company_context(
                "https://job-boards.greenhouse.io/transmarketgroup/jobs/5212335007"
            ),
        )
        self.assertEqual(
            "example",
            greenhouse._greenhouse_company_context(
                "https://boards.greenhouse.io/example/jobs/123"
            ),
        )
        self.assertEqual(
            "",
            greenhouse._greenhouse_company_context(
                "https://jobs.example.com/example/jobs/123"
            ),
        )
        self.assertEqual(
            "",
            greenhouse._greenhouse_company_context(
                "https://evilgreenhouse.io/example/jobs/123"
            ),
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
        self.assertTrue(
            qa.answer_requires_manual(
                control, "No", profile_text="", approved_answers={},
            )
        )
        self.assertFalse(
            qa.answer_requires_manual(
                control, "No", profile_text="disability: no", approved_answers={},
            )
        )

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
            {"dob", "pronouns", "hybrid", "offer"},
            {answer["id_or_name"] for answer in allowed},
        )
        self.assertEqual(
            {"product", "prior", "deadline", "comp"},
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
            {"referral-no"},
            {answer["id_or_name"] for answer in allowed},
        )
        self.assertEqual(
            {"referral-wrong", "household"},
            {answer["id_or_name"] for answer in blocked},
        )

    def test_filter_manual_answers_accepts_company_context_keyword(self):
        controls = [
            {"id": "availity", "label": "Are you a relative of an Availity employee? *", "options": ["Yes", "No"]},
        ]
        answers = [
            {"id_or_name": "availity", "answer": "No"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls,
            answers,
            company_context="Availity",
            approved_answers=APPROVED_AUG_25,
        )

        self.assertEqual(["availity"], [answer["id_or_name"] for answer in allowed])
        self.assertEqual([], blocked)

    def test_company_scoped_household_fact_blocks_mismatched_company_context(self):
        controls = [
            {"id": "relative", "label": "Are you a relative of an Availity employee? *", "options": ["Yes", "No"]},
            {"id": "household", "label": "Is any member of your household employed by Availity?", "options": ["Yes", "No"]},
            {"id": "family", "label": "Do you have a family member who works at Availity?", "options": ["Yes", "No"]},
        ]
        answers = qa.explicit_approved_answers(
            controls,
            company_context="Point72",
            approved_answers=APPROVED_AUG_25,
        )

        allowed, blocked = qa.filter_manual_answers(
            controls,
            answers,
            company_context="Point72",
            approved_answers=APPROVED_AUG_25,
        )

        self.assertEqual([], allowed)
        self.assertEqual(
            ["relative", "household", "family"],
            [answer["id_or_name"] for answer in blocked],
        )

    def test_company_scoped_household_fact_allows_exact_company_context(self):
        controls = [
            {"id": "relative", "label": "Are you a relative of an Availity employee? *", "options": ["Yes", "No"]},
            {"id": "household", "label": "Is any member of your household employed by Availity?", "options": ["Yes", "No"]},
            {"id": "family", "label": "Do you have a family member who works at Availity?", "options": ["Yes", "No"]},
        ]
        answers = qa.explicit_approved_answers(
            controls,
            company_context="Availity",
            approved_answers=APPROVED_AUG_25,
        )

        allowed, blocked = qa.filter_manual_answers(
            controls,
            answers,
            company_context="Availity",
            approved_answers=APPROVED_AUG_25,
        )

        self.assertEqual(
            ["relative", "household", "family"],
            [answer["id_or_name"] for answer in allowed],
        )
        self.assertEqual([], blocked)

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

    def test_large_greenhouse_menus_and_grad_ranges_use_grounded_facts(self):
        approved = {
            **self.APPROVED,
            "education": {
                **self.APPROVED["education"],
                "high_school": "Plano West Senior High School, Plano, Texas",
                "high_school_graduation_year": "2024",
            },
        }
        controls = [
            {
                "id": "citizenship",
                "label": "Please select the country where you hold citizenship / permanent residence.",
                "options": ["Afghanistan", "France", "United States of America"],
            },
            {
                "id": "high-school-date",
                "label": (
                    "You must have earned a high school diploma. Please confirm the "
                    "month and year that reflects your high school graduation date."
                ),
                "options": ["Spring/Summer 2025", "Spring/Summer 2024", "Spring/Summer 2023"],
            },
            {
                "id": "imc-grad",
                "label": "When is your anticipated graduation date - please select a Graduation Date range:",
                "options": ["August 2027 - December 2027", "January 2028 - July 2028"],
            },
            {
                "id": "jump-grad",
                "label": "What is your expected graduation date?",
                "options": ["Winter 2028", "Spring/Summer 2028", "Fall 2028"],
            },
            {
                "id": "university-country",
                "label": "Please select the location of your current university.",
                "options": ["France", "United States"],
            },
            {
                "id": "current-school",
                "label": "Please select your current school from the list below:",
                "options": ["Arizona State University", "Brown University"],
            },
        ]

        rendered = qa.explicit_approved_answers(controls, approved_answers=approved)
        answers = {item["id_or_name"]: item["answer"] for item in rendered}

        self.assertEqual("United States of America", answers["citizenship"])
        self.assertEqual("Spring/Summer 2024", answers["high-school-date"])
        self.assertEqual("January 2028 - July 2028", answers["imc-grad"])
        self.assertEqual("Spring/Summer 2028", answers["jump-grad"])
        self.assertEqual("United States", answers["university-country"])
        self.assertEqual("Brown University", answers["current-school"])
        allowed, blocked = qa.filter_manual_answers(
            controls, rendered, approved_answers=approved,
        )
        self.assertEqual(set(answers), {item["id_or_name"] for item in allowed})
        self.assertEqual([], blocked)
        self.assertIn("slice(0, 1000)", qa.EXTRACT_JS)
        self.assertIn("slice(0, 500)", qa.EXTRACT_JS)

    def test_year_only_facts_never_invent_months_or_replace_school_status(self):
        approved = {
            **self.APPROVED,
            "education": {
                **self.APPROVED["education"],
                "high_school_graduation_year": "2024",
            },
        }
        controls = [
            {
                "id": "hs-month",
                "label": "When did you earn your high school diploma?",
                "options": ["January 2024", "May 2024", "June 2024"],
            },
            {
                "id": "school-year",
                "label": "Please select your current school year from the list below",
                "options": ["Freshman", "Sophomore", "Junior", "Senior"],
            },
            {
                "id": "school-status",
                "label": "Choose your current school enrollment status",
                "options": ["Enrolled", "Graduated", "On leave"],
            },
            {
                "id": "grad-class",
                "label": "What is your graduation year?",
                "options": ["Class of 2027", "Class of 2028"],
            },
        ]

        rendered = qa.explicit_approved_answers(controls, approved_answers=approved)
        answers = {item["id_or_name"]: item["answer"] for item in rendered}

        self.assertNotIn("hs-month", answers)
        self.assertNotIn("school-year", answers)
        self.assertNotIn("school-status", answers)
        self.assertEqual("Class of 2028", answers["grad-class"])

        allowed, blocked = qa.filter_manual_answers(
            controls,
            [
                {"id_or_name": "hs-month", "answer": "January 2024"},
                {"id_or_name": "school-year", "answer": "Brown University"},
                {"id_or_name": "school-status", "answer": "Brown University"},
                {"id_or_name": "grad-class", "answer": "Class of 2028"},
            ],
            approved_answers=approved,
        )
        self.assertEqual(
            ["grad-class"], [item["id_or_name"] for item in allowed],
        )
        self.assertEqual(
            {"hs-month", "school-year", "school-status"},
            {item["id_or_name"] for item in blocked},
        )

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

        company_contexts = {
            "nextiva-employee": "Nextiva",
            "nextiva-onsite": "Nextiva",
            "akuna-application": "Akuna Capital",
        }
        policy_controls = [
            dict(item, company_context=company_contexts.get(item["id"], ""))
            for item in controls
        ]
        rendered = []
        for control in policy_controls:
            rendered.extend(qa.explicit_approved_answers(
                [control],
                company_context=control["company_context"],
                approved_answers=approved,
            ))

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
            policy_controls,
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
            [dict(item, company_context=company_contexts.get(item["id"], ""))
             for item in wrong_controls],
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
                "id": "notice-only-boolean",
                "label": "Do you have a notice period?",
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
            "notice-only-boolean": "No",
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
            "notice-only-boolean": "Yes",
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

    def test_number_eighteen_is_not_mistaken_for_an_age_question(self):
        controls = [
            {
                "id": "experience",
                "label": "Do you have 18 months of professional work experience?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "lifting",
                "label": "Can you lift 18 kg?",
                "options": ["Yes", "No"],
                "value": "",
            },
        ]
        self.assertEqual([], qa.explicit_approved_answers(
            controls, approved_answers=self.APPROVED,
        ))
        for control in controls:
            self.assertFalse(qa._blocked_answer_is_approved(
                control, "Yes", self.APPROVED,
            ))

        actual_age = {
            "id": "age",
            "label": "Are you 18 or older?",
            "options": ["Yes", "No"],
            "value": "",
        }
        self.assertEqual(
            [{"id_or_name": "age", "answer": "Yes"}],
            qa.explicit_approved_answers([actual_age], approved_answers=self.APPROVED),
        )

    def test_company_facts_do_not_cross_company_or_word_boundaries(self):
        self.assertTrue(qa._company_matches("Sentry", "Have you used Sentry?"))
        self.assertFalse(qa._company_matches("Meta", "Describe your metadata work."))
        self.assertFalse(qa._company_matches(
            "Crowe", "Have you worked here before?", "Crowell & Moring",
        ))
        self.assertFalse(qa._company_matches(
            "Sentry", "Have you used our platform?", "Sentry Insurance",
        ))
        self.assertTrue(qa._company_matches(
            "Sentry", "Have you used our platform?", "Sentry",
        ))

    def test_confirmed_standardized_test_answers_render_and_validate_exactly(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["education"]["standardized_tests"] = {
            "sat": 1570,
            "act": None,
            "act_taken": False,
        }
        controls = [
            {
                "id": "type",
                "label": "Select your Standardized Test score type",
                "options": ["ACT", "SAT"],
            },
            {
                "id": "sat",
                "label": "SAT score",
                "options": ["1560 out of 1600", "1570 out of 1600"],
            },
            {
                "id": "act",
                "label": "ACT score",
                "options": ["36 out of 36", "Did not take"],
            },
            {
                "id": "sat-verb",
                "label": "Have you sat for any professional exams?",
            },
            {
                "id": "act-law",
                "label": "Fair Credit Reporting Act disclosure",
            },
        ]

        rendered = qa.explicit_approved_answers(controls, approved_answers=approved)
        self.assertEqual(
            {
                "type": "SAT",
                "sat": "1570 out of 1600",
                "act": "Did not take",
            },
            {item["id_or_name"]: item["answer"] for item in rendered},
        )
        allowed, blocked = qa.filter_manual_answers(
            controls,
            [
                {"id_or_name": "sat", "answer": "1570 out of 1600"},
                {"id_or_name": "act", "answer": "Did not take"},
                {"id_or_name": "act", "answer": "36 out of 36"},
            ],
            approved_answers=approved,
        )
        self.assertEqual(2, len(allowed))
        self.assertEqual(["36 out of 36"], [item["answer"] for item in blocked])

    def test_bitter_lesson_answer_requires_the_exact_confirmed_prompt(self):
        approved = json.loads(json.dumps(self.APPROVED))
        answer = (
            "The Bitter Lesson. I re-read it after hearing Boris Turney discuss "
            "during a YC talk how its ideas could apply to our YC startups."
        )
        approved["long_form_answers"].append({
            "key": "recent_interesting_reading",
            "match_all": [
                "most interesting paper", "blog post", "documentation", "past month",
            ],
            "answer": answer,
        })
        exact = {
            "id": "reading",
            "label": (
                "What's the most interesting paper, blog post, or documentation "
                "you've read in the past month?"
            ),
        }
        similar = {
            "id": "favorite",
            "label": "What is your favorite paper about PPO?",
        }

        self.assertEqual(
            [{"id_or_name": "reading", "answer": answer}],
            qa.explicit_approved_answers(
                [exact, similar], approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            exact, answer, approved_answers=approved,
        ))
        self.assertNotEqual(
            answer,
            next(iter([
                item["answer"] for item in qa.explicit_approved_answers(
                    [similar], approved_answers=approved,
                )
            ]), None),
        )

    def test_global_onsite_and_relocation_policy_selects_relocation_not_commute(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["preferences"].update({"onsite": True, "relocate": True})
        onsite_option = "I’m open to relocating to the area and working onsite"
        onsite = {
            "id": "onsite",
            "label": (
                "This role is based at Nextiva’s Scottsdale headquarters and reflects "
                "our in-office approach. How does this align with your ability to work onsite?"
            ),
            "options": [
                "I’m within commuting distance and able to work onsite",
                onsite_option,
                "I’m not able to work onsite and am interested in fully remote roles",
            ],
        }
        relocate = {
            "id": "relocate",
            "label": "Are you willing to relocate for this role?",
            "options": ["Yes", "No"],
        }
        commute = {
            "id": "commute",
            "label": "Can you work onsite and are you within commuting distance?",
            "options": [
                "Yes, I am within commuting distance",
                "No, I cannot work onsite",
            ],
        }
        requires_hybrid = {
            "id": "requires-hybrid",
            "label": "Do you require a hybrid schedule?",
            "options": ["Yes", "No"],
        }

        rendered = qa.explicit_approved_answers(
            [onsite, relocate, commute, requires_hybrid], approved_answers=approved,
        )
        self.assertEqual(
            {"onsite": onsite_option, "relocate": "Yes"},
            {item["id_or_name"]: item["answer"] for item in rendered},
        )
        self.assertFalse(qa.answer_requires_manual(
            onsite, onsite_option, approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            onsite,
            "I’m within commuting distance and able to work onsite",
            approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            commute,
            "Yes, I am within commuting distance",
            approved_answers=approved,
        ))

    def test_references_available_upon_request_does_not_invent_contact_details(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["professional"] = {"references": "Available upon request."}
        references = {"id": "refs", "label": "References"}
        email = {"id": "email", "label": "Reference email address"}

        self.assertEqual(
            [{"id_or_name": "refs", "answer": "Available upon request."}],
            qa.explicit_approved_answers(
                [references, email], approved_answers=approved,
            ),
        )
        self.assertFalse(qa.answer_requires_manual(
            references, "Available upon request.", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            email, "invented@example.com", approved_answers=approved,
        ))

    def test_stepstone_no_referral_renders_na_only_for_conditional_name(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["company_facts"]["StepStone"] = {"referral": False}
        referral = {
            "id": "referral",
            "label": "Were you referred by a StepStone employee?",
            "options": ["Yes", "No"],
        }
        detail = {
            "id": "detail",
            "label": (
                "If referred by a StepStone employee, please list their full name below."
            ),
        }

        rendered = qa.explicit_approved_answers(
            [referral, detail],
            company_context="StepStone",
            approved_answers=approved,
        )
        self.assertEqual(
            {"referral": "No", "detail": "N/A"},
            {item["id_or_name"]: item["answer"] for item in rendered},
        )
        scoped_detail = dict(detail, company_context="StepStone")
        self.assertFalse(qa.answer_requires_manual(
            scoped_detail, "N/A", approved_answers=approved,
        ))
        self.assertTrue(qa.answer_requires_manual(
            scoped_detail, "Jane Doe", approved_answers=approved,
        ))
        generic_detail = dict(
            detail,
            label="If referred by an employee, please list their full name below.",
        )
        self.assertEqual([], qa.explicit_approved_answers(
            [generic_detail],
            company_context="Other Company",
            approved_answers=approved,
        ))

    def test_plural_outstanding_offers_and_university_reconfirmation_are_deterministic(self):
        controls = [
            {
                "id": "offers",
                "label": "Do you currently have any outstanding offers?",
                "options": ["Yes", "No"],
            },
            {
                "id": "school",
                "label": "Please re-confirm the university you currently attend",
                "options": ["Brown University", "Boston University"],
            },
            {
                "id": "school-email",
                "label": "Please confirm your university email address",
                "options": ["Brown University", "Boston University"],
            },
        ]
        self.assertEqual(
            {"offers": "Yes", "school": "Brown University"},
            {
                item["id_or_name"]: item["answer"]
                for item in qa.explicit_approved_answers(
                    controls, approved_answers=self.APPROVED,
                )
            },
        )

    def test_similarly_named_school_is_not_selected(self):
        approved = json.loads(json.dumps(self.APPROVED))
        approved["education"]["high_school"] = (
            "Plano West Senior High School, Plano, Texas"
        )
        self.assertIsNone(qa._approved_high_school_answer(
            approved, ["Plano Senior High School", "Plano East Senior High School"],
        ))
        self.assertEqual(
            "Plano West Senior High School",
            qa._approved_high_school_answer(
                approved,
                ["Plano West Senior High School", "Plano Senior High School"],
            ),
        )

    def test_pay_expectations_cannot_accept_an_invented_number(self):
        control = {
            "id": "pay",
            "label": "What are your pay expectations?",
            "value": "",
        }
        self.assertTrue(qa.answer_requires_manual(
            control, "$55/hour", approved_answers=self.APPROVED,
        ))
        self.assertFalse(qa.answer_requires_manual(
            control, "Open to the employer-published range and market rate.",
            approved_answers=self.APPROVED,
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

        self.assertEqual([dict(model_answers[0], label="Why us?")], answers)
        self.assertEqual(["allowed", "blocked_manual"], [r["decision"] for r in rows])
        self.assertEqual(["Why us?", "Desired compensation"], [r["question"] for r in rows])
        self.assertEqual(["acme", "acme"], [r["slug"] for r in rows])
        self.assertEqual(["https://example.test/job", "https://example.test/job"], [r["url"] for r in rows])


if __name__ == "__main__":
    unittest.main()


def test_extract_js_groups_nameless_redwood_radios_by_human_ancestor_label():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content("""
                <div role="radiogroup" aria-labelledby="redwood-question">
                  <div id="redwood-question">Are you authorized to work in the United States?</div>
                  <div class="oj-flex"><label><input type="radio"> Yes</label></div>
                  <div class="oj-flex"><label><input type="radio"> No</label></div>
                </div>
            """)
            controls = page.evaluate(qa.EXTRACT_JS)
        finally:
            browser.close()

    assert len(controls) == 1
    assert controls[0]["label"] == "Are you authorized to work in the United States?"
    assert controls[0]["options"] == ["Yes", "No"]


def test_fill_answers_standalone_radio_no_without_click_is_failed_not_filled():
    class Keyboard:
        def press(self, key): pass
    class El:
        def scroll_into_view_if_needed(self, timeout=None): pass
        def evaluate(self, script, *args): return False
        def check(self): raise AssertionError('No answer must not call check')
    class Loc:
        @property
        def first(self): return El()
    class Page:
        keyboard = Keyboard()
        def locator(self, selector): return Loc()
        def wait_for_timeout(self, value): pass
    controls = [{"id": "r-no", "name": "", "tag": "input", "type": "radio", "label": "No", "value": "", "chosen": ""}]
    filled, failed = qa.fill_answers(Page(), controls, [{"id_or_name": "r-no", "answer": "No"}])
    assert filled == []
    assert failed == ["No"]


def test_oracle_redwood_combobox_control_is_not_plain_text():
    src = qa.EXTRACT_JS
    assert "role || el.type" in src
