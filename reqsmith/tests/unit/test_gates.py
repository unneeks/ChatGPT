"""Rule-table coverage: every YAML gate rule has at least one pass and one fail fixture."""

from reqsmith.verification.gates import classify_risk_tier, evaluate, load_policy_pack

GOOD_INTAKE = {
    "summary": "Customer onboarding revamp",
    "description": "Digitise the retail onboarding intake flow so that branch staff capture "
    "structured data once and downstream teams stop re-keying information into legacy systems.",
    "reporter": "sponsor@bank.com",
}

GOOD_STORY = {
    "title": "Capture intake form",
    "story": "As a branch officer, I want a structured intake form, so that data is captured once",
    "acceptance_criteria": ["Given a new applicant When the form is submitted Then a record is created"],
    "citations": [{"source_id": "abc", "span_start": 0, "span_end": 10}],
}

GOOD_DRAFT = {"epic_summary": "Onboarding", "stories": [GOOD_STORY]}


def _verdict(report, rule_id):
    return next(r.verdict for r in report.results if r.rule_id == rule_id)


# --- intake rules ---

def test_intake_rules_pass_and_fail():
    passing = evaluate(GOOD_INTAKE, applies_to="intake")
    assert passing.passed

    assert _verdict(evaluate({**GOOD_INTAKE, "summary": ""}, applies_to="intake"),
                    "intake.summary.present") == "fail"
    assert _verdict(evaluate({**GOOD_INTAKE, "description": "too short"}, applies_to="intake"),
                    "intake.description.min_length") == "fail"
    assert _verdict(evaluate({**GOOD_INTAKE, "reporter": ""}, applies_to="intake"),
                    "intake.reporter.present") == "fail"


# --- draft rules ---

def test_draft_rules_pass():
    assert evaluate(GOOD_DRAFT, applies_to="draft").passed


def test_draft_placeholder_fails():
    bad = {**GOOD_DRAFT, "epic_summary": "Onboarding TBD"}
    assert _verdict(evaluate(bad, applies_to="draft"), "draft.no_placeholders") == "fail"


def test_draft_without_stories_fails():
    assert _verdict(evaluate({"stories": []}, applies_to="draft"),
                    "draft.stories.present") == "fail"


def test_draft_non_gwt_ac_fails():
    bad_story = {**GOOD_STORY, "acceptance_criteria": ["it should just work"]}
    report = evaluate({"stories": [bad_story]}, applies_to="draft")
    assert _verdict(report, "draft.acceptance_criteria.gwt") == "fail"


def test_draft_without_citations_fails():
    bad_story = {**GOOD_STORY, "citations": []}
    report = evaluate({"stories": [bad_story]}, applies_to="draft")
    assert _verdict(report, "draft.citations.present") == "fail"


# --- PII / secrets (applies_to: any) ---

def test_pii_account_number_blocks():
    bad = {**GOOD_INTAKE, "description": GOOD_INTAKE["description"] + " account 12345678901234"}
    assert _verdict(evaluate(bad, applies_to="intake"), "text.pii.account_number") == "fail"


def test_pii_ssn_blocks():
    bad = {**GOOD_INTAKE, "description": GOOD_INTAKE["description"] + " ssn 123-45-6789"}
    assert _verdict(evaluate(bad, applies_to="intake"), "text.pii.ssn_like") == "fail"


def test_secret_material_blocks():
    bad = {**GOOD_DRAFT, "epic_summary": "uses api_key=sk-12345 internally"}
    assert _verdict(evaluate(bad, applies_to="draft"), "text.secrets") == "fail"


def test_clean_payload_passes_pii_rules():
    report = evaluate(GOOD_INTAKE, applies_to="intake")
    assert _verdict(report, "text.pii.account_number") == "pass"
    assert _verdict(report, "text.pii.ssn_like") == "pass"
    assert _verdict(report, "text.secrets") == "pass"


def test_every_rule_in_pack_is_exercised():
    """Guard: adding a rule to gates-v1.yaml without a fixture here fails CI."""
    pack = load_policy_pack("gates")
    tested = {
        "intake.summary.present", "intake.description.min_length", "intake.reporter.present",
        "draft.no_placeholders", "draft.stories.present", "draft.acceptance_criteria.gwt",
        "draft.citations.present", "text.pii.account_number", "text.pii.ssn_like", "text.secrets",
    }
    assert {r["id"] for r in pack["rules"]} == tested


# --- risk tiers (deterministic, LLM never sets these) ---

def test_risk_tier_rules():
    assert classify_risk_tier("KYC remediation for AML compliance")[0] == "high"
    assert classify_risk_tier("new payments settlement flow")[0] == "high"
    assert classify_risk_tier("update internal tool wording change")[0] == "low"
    assert classify_risk_tier("improve branch reporting dashboard for staff")[0] == "medium"
