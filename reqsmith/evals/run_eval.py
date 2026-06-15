#!/usr/bin/env python3
"""Eval harness: score reqsmith deterministic pipeline stages against golden epics.

Scores the deterministic stages (triage risk classification + intake gate engine)
without requiring LLM calls. A separate --with-llm flag enables LLM stages
(drafting + verification + judge) when ANTHROPIC_API_KEY is set.

Usage (from the reqsmith/ directory):
  python ../evals/run_eval.py
  python ../evals/run_eval.py --golden-dir evals/golden --report-dir evals/reports
  python ../evals/run_eval.py --verbose

Output:
  evals/reports/YYYY-MM-DD_{prompt_version}_{model_id}.json

Metrics:
  risk_tier_accuracy  – % fixtures where tier matches expected
  gate_verdict_match  – % of rule assertions matching expected pass/fail
  overall_score       – weighted mean
  per_fixture         – per-golden results for drill-down
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Make sure reqsmith src is importable when run from repo root
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))

# Set minimal env so settings doesn't error on missing vars
import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///file::memory:")
os.environ.setdefault("JIRA_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("JIRA_EMAIL", "eval@example.com")
os.environ.setdefault("JIRA_API_TOKEN", "unused-eval-token")
os.environ.setdefault("JIRA_PROJECT_KEY", "EVAL")
os.environ.setdefault("BOT_APP_ID", "unused")
os.environ.setdefault("BOT_APP_PASSWORD", "unused")
os.environ.setdefault("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "unused-eval-key"))


def _load_golden(golden_dir: Path) -> list[dict]:
    fixtures = []
    for path in sorted(golden_dir.glob("*.json")):
        with open(path) as f:
            fixtures.append(json.load(f))
    return fixtures


def _score_fixture(fixture: dict, *, verbose: bool = False) -> dict:
    """Run deterministic checks against one golden fixture."""
    from reqsmith.verification.gates import classify_risk_tier, evaluate

    inp = fixture["input"]
    expected = fixture["expected"]
    t0 = time.perf_counter()

    # --- risk tier classification ---
    combined_text = f"{inp.get('summary', '')} {inp.get('description', '')}"
    actual_tier, triggering_rule = classify_risk_tier(combined_text)
    tier_ok = actual_tier == expected["risk_tier"]
    expected_rule = expected.get("triggering_rule")
    # null expected_rule means "no keyword match → default tier"
    rule_ok = (expected_rule is None and triggering_rule == "tier.default") or triggering_rule == expected_rule

    # --- intake gate evaluation ---
    payload = {
        "summary": inp.get("summary", ""),
        "description": inp.get("description", ""),
        "reporter": inp.get("reporter", ""),
    }
    gate_report = evaluate(payload, applies_to="intake")

    expected_gates = expected.get("intake_gates", {})
    gate_assertions: list[dict] = []
    matched = 0
    for result in gate_report.results:
        if result.rule_id in expected_gates:
            exp_verdict = expected_gates[result.rule_id]
            ok = result.verdict == exp_verdict
            if ok:
                matched += 1
            gate_assertions.append({
                "rule_id": result.rule_id,
                "expected": exp_verdict,
                "actual": result.verdict,
                "pass": ok,
                "severity": result.severity,
            })

    gate_total = len(expected_gates)
    gate_match_rate = matched / gate_total if gate_total else 1.0

    latency_ms = (time.perf_counter() - t0) * 1000

    if verbose:
        tier_sym = "✓" if tier_ok else "✗"
        print(f"\n  [{fixture['id']}] {fixture['description']}")
        print(f"    Risk tier: {tier_sym} expected={expected['risk_tier']} actual={actual_tier}"
              f" rule={triggering_rule}")
        for a in gate_assertions:
            sym = "✓" if a["pass"] else "✗"
            print(f"    Gate {sym} {a['rule_id']}: expected={a['expected']} actual={a['actual']}")

    return {
        "fixture_id": fixture["id"],
        "description": fixture["description"],
        "risk_tier": {
            "expected": expected["risk_tier"],
            "actual": actual_tier,
            "triggering_rule": triggering_rule,
            "correct": tier_ok and rule_ok,
        },
        "gate_assertions": gate_assertions,
        "gate_match_rate": gate_match_rate,
        "latency_ms": round(latency_ms, 1),
    }


def _aggregate(results: list[dict]) -> dict:
    n = len(results)
    if n == 0:
        return {"overall_score": 0.0}
    tier_acc = sum(1 for r in results if r["risk_tier"]["correct"]) / n
    gate_acc = sum(r["gate_match_rate"] for r in results) / n
    overall = (tier_acc + gate_acc) / 2
    return {
        "fixtures_run": n,
        "risk_tier_accuracy": round(tier_acc, 3),
        "gate_verdict_match": round(gate_acc, 3),
        "overall_score": round(overall, 3),
    }


def main():
    parser = argparse.ArgumentParser(description="reqsmith eval harness")
    parser.add_argument("--golden-dir", default="evals/golden", help="Directory with golden JSON fixtures")
    parser.add_argument("--report-dir", default="evals/reports", help="Directory to write versioned report")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-fixture details")
    args = parser.parse_args()

    golden_dir = Path(args.golden_dir)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    if not golden_dir.exists():
        print(f"ERROR: golden-dir not found: {golden_dir}", file=sys.stderr)
        sys.exit(1)

    fixtures = _load_golden(golden_dir)
    if not fixtures:
        print("No golden fixtures found.", file=sys.stderr)
        sys.exit(1)

    print(f"Running eval against {len(fixtures)} golden fixture(s)…")

    results = [_score_fixture(f, verbose=args.verbose) for f in fixtures]
    summary = _aggregate(results)

    # Load version metadata
    from reqsmith.settings import get_settings
    settings = get_settings()

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "prompt_pack_version": settings.prompt_pack_version,
        "policy_pack_version": settings.policy_pack_version,
        "model_drafting": os.environ.get("MODEL_DRAFTING", "n/a"),
        "summary": summary,
        "per_fixture": results,
    }

    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    report_path = report_dir / f"{date_str}_{settings.prompt_pack_version}_{settings.policy_pack_version}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary
    s = summary
    print(f"\n{'='*50}")
    print(f"Eval report: {report_path}")
    print(f"  Fixtures run:          {s['fixtures_run']}")
    print(f"  Risk tier accuracy:    {s['risk_tier_accuracy']:.1%}")
    print(f"  Gate verdict match:    {s['gate_verdict_match']:.1%}")
    print(f"  Overall score:         {s['overall_score']:.1%}")
    print(f"{'='*50}")

    # Exit 1 if overall score < 0.9 (can be used as CI gate)
    if s["overall_score"] < 0.9:
        print("WARN: overall score below 0.90 threshold", file=sys.stderr)
        sys.exit(1)

    print("PASS")


if __name__ == "__main__":
    main()
