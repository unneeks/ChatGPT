"""Layer-1 deterministic policy gate engine.

Loads versioned YAML rule packs and evaluates them in code. Design test (§1):
"can an agent violate this rule by accident?" — no, because stage code calls
evaluate() and a blocking failure stops the pipeline regardless of model output.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from reqsmith.settings import get_settings


@dataclass
class RuleResult:
    rule_id: str
    verdict: str  # pass|fail
    severity: str
    message: str


@dataclass
class GateReport:
    policy_version: str
    results: list[RuleResult]

    @property
    def blocking_failures(self) -> list[RuleResult]:
        return [r for r in self.results if r.verdict == "fail" and r.severity == "block"]

    @property
    def passed(self) -> bool:
        return not self.blocking_failures


def _all_text(payload: dict) -> str:
    parts: list[str] = []

    def walk(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(payload)
    return "\n".join(parts)


GWT_PATTERN = re.compile(r"(?is)\bgiven\b.+\bwhen\b.+\bthen\b")


def _evaluate_rule(rule: dict, payload: dict) -> str:
    rule_type = rule["type"]
    field = rule.get("field", "")
    value = _all_text(payload) if field == "__all_text__" else payload.get(field)

    if rule_type == "required_field":
        ok = bool(value) and (not isinstance(value, list | dict) or len(value) > 0)
    elif rule_type == "min_length":
        ok = isinstance(value, str) and len(value.strip()) >= rule["min"]
    elif rule_type == "regex_absent":
        ok = not re.search(rule["pattern"], value or "")
    elif rule_type == "gwt_shape":
        stories = value or []
        ok = bool(stories) and all(
            GWT_PATTERN.search(ac)
            for story in stories
            for ac in story.get("acceptance_criteria", [])
        ) and all(story.get("acceptance_criteria") for story in stories)
    elif rule_type == "citation_presence":
        stories = value or []
        ok = bool(stories) and all(story.get("citations") for story in stories)
    else:
        raise ValueError(f"unknown rule type '{rule_type}' in {rule['id']}")
    return "pass" if ok else "fail"


@lru_cache
def load_policy_pack(name: str, policy_dir: str | None = None) -> dict:
    directory = Path(policy_dir) if policy_dir else get_settings().policy_dir
    settings = get_settings()
    path = directory / f"{name}-{settings.policy_pack_version}.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def evaluate(payload: dict, *, applies_to: str) -> GateReport:
    """Run all gates whose applies_to matches ('any' rules always run)."""
    pack = load_policy_pack("gates")
    results = []
    for rule in pack["rules"]:
        if rule["applies_to"] not in (applies_to, "any"):
            continue
        verdict = _evaluate_rule(rule, payload)
        results.append(
            RuleResult(
                rule_id=rule["id"],
                verdict=verdict,
                severity=rule.get("severity", "block"),
                message=rule.get("message", ""),
            )
        )
    return GateReport(policy_version=pack["version"], results=results)


def classify_risk_tier(text: str) -> tuple[str, str]:
    """Deterministic tier from keyword rules. Returns (tier, rule_id)."""
    pack = load_policy_pack("risk-tiers")
    lowered = text.lower()
    for rule in pack["rules"]:
        if any(keyword.lower() in lowered for keyword in rule["keywords"]):
            return rule["tier"], rule["id"]
    return pack["default_tier"], "tier.default"
