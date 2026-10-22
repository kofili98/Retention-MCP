import datetime
import json
import os
from pathlib import Path
from typing import Callable

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from agent.claim_checker import grade_report
from server.mcp_server import (
    get_at_risk_customers,
    get_churn_summary,
    get_cohort_retention,
    get_run_manifest,
    run_data_health_check,
    shopify_data_health_check,
    shopify_subscription_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / ".env.example")

REPORTS_DIR = REPO_ROOT / "reports"
REPORT_MD_PATH = REPORTS_DIR / "weekly_retention_report.md"
TOOL_LOG_PATH = REPORTS_DIR / "weekly_report_tool_log.json"
CLAIMS_JSON_PATH = REPORTS_DIR / "claims.json"
CLAIM_GRADE_PATH = REPORTS_DIR / "claim_check_report.json"

# Strict read-only tool registry
READ_ONLY_TOOLS = {
    "run_data_health_check": run_data_health_check,
    "get_run_manifest": get_run_manifest,
    "get_churn_summary": get_churn_summary,
    "get_cohort_retention": get_cohort_retention,
    "get_at_risk_customers": get_at_risk_customers,
    "shopify_subscription_snapshot": shopify_subscription_snapshot,
    "shopify_data_health_check": shopify_data_health_check,
}


def execute_logged_tool(name: str, tool_log: list, **kwargs) -> dict:
    if name not in READ_ONLY_TOOLS:
        raise PermissionError(f"Tool '{name}' is not in the read-only whitelist.")
    result = READ_ONLY_TOOLS[name](**kwargs)
    tool_log.append(
        {
            "step": len(tool_log) + 1,
            "tool_name": name,
            "arguments": kwargs,
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )
    return result


def _build_deterministic_narrative(
    health: dict,
    churn_90: dict,
    at_risk: dict,
    shopify_snap: dict,
    shopify_health: dict,
) -> str:
    rec = health["reconciliation"]
    sel = churn_90["selected_threshold_result"]
    return f"""## 1. Data Health & Exclusions Ledger
- **Status:** `{health['status']}` (Exact Reconciliation: `{rec['reconciled_exactly']}`)
- **Raw Rows:** `{rec['total_raw_rows']:,}` | **Excluded Rows:** `{rec['total_excluded_rows']:,}` | **Clean Analysis Rows:** `{rec['final_analysis_rows']:,}`
- **Primary Exclusions:** Missing Customer ID (`{rec['exclusions_by_rule'].get('2_MISSING_CUSTOMER_ID', 0):,}`), Exact Duplicates (`{rec['exclusions_by_rule'].get('1_EXACT_DUPLICATE', 0):,}`), Cancellations (`{rec['exclusions_by_rule'].get('3_CANCELLATION_INVOICE', 0):,}`).

## 2. Inferred Churn & Sensitivity Context (Anchored to {churn_90['anchor_date_t_max']})
- **90-Day Inferred Churn Rate:** `{sel['inferred_churn_rate']*100:.2f}%` (95% CI: `[{sel['ci_95_lower']*100:.2f}%, {sel['ci_95_upper']*100:.2f}%]`) across `{sel['eligible_uncensored_customers']:,}` uncensored customers (`{sel['right_censored_customers']:,}` right-censored).
- **Empirical Inter-Purchase Gap:** Median `{churn_90['inter_purchase_gap_distribution_days']['p50_median']}` days, 75th percentile `{churn_90['inter_purchase_gap_distribution_days']['p75']}` days.
- **Caveat:** Because UCI Online Retail II is non-contractual gift-ware data, inactivity does not prove permanent defection. What the data does not support: we cannot infer why customers paused purchasing or generalize non-contractual gift rhythms to SaaS/subscription billing.

## 3. At-Risk Customers & Testable Experiment Hypothesis
- **At-Risk Population:** `{at_risk['total_at_risk_count']:,}` repeat customers (`>= 3` purchase days) have exceeded their personal median cadence (`> max(1.5x median, median + 14d)`), representing `£{at_risk['total_at_risk_lifetime_revenue']:,.2f}` in historical revenue.
- **Proposed Experiment:**
  - **Hypothesis:** Sending a personalized restock reminder triggered at `1.5x` a repeat buyer's median inter-purchase gap will increase 30-day reactivation compared to no outreach.
  - **Target Population:** Customers flagged in `get_at_risk_customers` randomized 50/50 into Treatment (cadence-triggered reminder) vs. Control (holdout, no outreach).
  - **Primary Metric:** 30-day Reactivation Rate (at least one valid non-cancelled invoice within 30 days of trigger) and Net Incremental Revenue per Customer (net of `C`-prefixed returns).

## 4. Isolated Shopify Subscription Layer
- **Contract Snapshot (`{shopify_snap['data_source']}`):** `{shopify_snap['total_contracts']}` total contracts (`{shopify_snap['status_breakdown']}`).
- **Integrity Checks (`{shopify_health['status']}`):** Flagged `{shopify_health['checks'][0]['failing_count']}` selling-plan order missing a contract ID and `{shopify_health['checks'][1]['failing_count']}` ACTIVE contract missing `nextBillingDate`."""


def _revise_narrative_once(
    draft_narrative: str,
    grade_result: dict,
    evidence_bundle: dict,
    report_type_label: str,
    api_key: str,
) -> str:
    if api_key and not api_key.startswith("sk-ant-..."):
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        revision_prompt = (
            f"Your previous draft of the {report_type_label} failed deterministic claim verification.\n"
            "Fix every arithmetic mismatch, methodological violation, and uncovered sentence listed below "
            "using ONLY the verified tool outputs.\n\n"
            f"PREVIOUS DRAFT:\n{draft_narrative}\n\n"
            f"CLAIM VERIFICATION FAILURES:\n{json.dumps(grade_result['failures'], indent=2)}\n\n"
            f"UNCOVERED SENTENCES:\n{json.dumps(grade_result['coverage']['uncovered_sentences'], indent=2)}\n\n"
            f"VERIFIED TOOL OUTPUTS:\n{json.dumps(evidence_bundle, indent=2)}"
        )
        msg = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            max_tokens=1800,
            temperature=0.0,
            messages=[{"role": "user", "content": revision_prompt}],
        )
        return msg.content[0].text

    return _build_deterministic_narrative(
        evidence_bundle["health"],
        evidence_bundle["churn_summary"],
        evidence_bundle["at_risk_summary"],
        evidence_bundle["shopify_snapshot"],
        evidence_bundle["shopify_health"],
    )


def generate_weekly_report(
    use_fixture: bool = True,
    draft_override_fn: Callable[[dict], str] | None = None,
    revision_override_fn: Callable[[str, dict, dict], str] | None = None,
) -> dict:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tool_log = []

    # 1. Mandatory Gate: Health Check & Manifest First
    health = execute_logged_tool("run_data_health_check", tool_log)
    manifest = execute_logged_tool("get_run_manifest", tool_log)

    if health["status"] == "CRITICAL" or not health["reconciliation"]["reconciled_exactly"]:
        abort_msg = (
            "# CRITICAL DATA HEALTH FAILURE — REPORT ABORTED\n\n"
            f"Health check failed reconciliation: `{json.dumps(health['reconciliation'])}`"
        )
        with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
            f.write(abort_msg)
        return {"status": "ABORTED_HEALTH", "passed": False}

    # 2. Fixed Execution Sequence: Churn -> Cohort Retention -> At-Risk -> Shopify
    churn_90 = execute_logged_tool("get_churn_summary", tool_log, inactivity_days=90)
    cohorts_buyer = execute_logged_tool(
        "get_cohort_retention", tool_log, cohort_by="buyer_type", period_months=6
    )
    at_risk = execute_logged_tool("get_at_risk_customers", tool_log, limit=10)
    shopify_snap = execute_logged_tool(
        "shopify_subscription_snapshot", tool_log, use_fixture=use_fixture
    )
    shopify_health = execute_logged_tool(
        "shopify_data_health_check", tool_log, use_fixture=use_fixture
    )

    with open(TOOL_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(tool_log, f, indent=2)

    evidence_bundle = {
        "health": health,
        "churn_summary": churn_90,
        "cohort_retention_sample": cohorts_buyer["records"][:30],
        "at_risk_summary": at_risk,
        "shopify_snapshot": shopify_snap,
        "shopify_health": shopify_health,
    }

    is_live_shopify = str(shopify_snap.get("data_source", "")).startswith("shopify_live:")
    report_type_label = (
        "reproducible retention report (static UCI benchmark + live weekly Shopify snapshot)"
        if is_live_shopify
        else "reproducible retention report"
    )
    report_title = (
        "# Reproducible Retention Report (Static UCI Benchmark + Weekly Live Shopify Layer)"
        if is_live_shopify
        else "# Reproducible Retention Report"
    )
    manifest_stamp = (
        "\n\n---\n### Cryptographic Run Manifest Stamp\n"
        f"```json\n{json.dumps(manifest, indent=2)}\n```\n"
    )

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if draft_override_fn is not None:
        narrative_v1 = draft_override_fn(evidence_bundle)
    elif api_key and not api_key.startswith("sk-ant-..."):
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        prompt = (
            f"You are a rigorous Data Integrity & Retention Analyst. Write a concise {report_type_label} "
            "strictly using the verified JSON tool outputs below.\n"
            "Requirements:\n"
            "1. State exact numbers from the exclusions ledger and health check first.\n"
            "2. Report the inferred churn numbers across the sensitivity table with 95% CIs and right-censoring caveats.\n"
            "3. Compare Wholesale_Proxy vs Retail_Proxy retention and explicitly state when the data does not support a causal conclusion.\n"
            "4. Propose ONE testable experiment as a formal hypothesis with a named primary metric and control group targeting the At-Risk cohort.\n"
            "5. Summarize the isolated Shopify subscription health findings without comparing or merging them with the UCI dataset.\n\n"
            f"VERIFIED TOOL OUTPUTS:\n{json.dumps(evidence_bundle, indent=2)}"
        )
        msg = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            max_tokens=1800,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        narrative_v1 = msg.content[0].text
    else:
        narrative_v1 = _build_deterministic_narrative(
            health, churn_90, at_risk, shopify_snap, shopify_health
        )

    full_report_v1 = f"{report_title}\n\n{narrative_v1}{manifest_stamp}"

    # 3. Claim Checker Gate (Pass 1)
    grade_v1 = grade_report(full_report_v1, evidence_bundle)
    (REPORTS_DIR / "weekly_retention_report_v1.md").write_text(full_report_v1, encoding="utf-8")

    audit_log = {
        "attempt_1": {
            "passed": grade_v1["passed"],
            "summary": grade_v1["summary"],
            "failures": grade_v1["failures"],
            "uncovered_sentences": grade_v1["coverage"]["uncovered_sentences"],
        },
        "revision_triggered": False,
        "final_passed": grade_v1["passed"],
    }
    final_report_md = full_report_v1
    final_grade = grade_v1

    # 4. Single Revision Pass if Attempt 1 Fails
    if not grade_v1["passed"]:
        audit_log["revision_triggered"] = True
        if revision_override_fn is not None:
            narrative_v2 = revision_override_fn(narrative_v1, grade_v1, evidence_bundle)
        else:
            narrative_v2 = _revise_narrative_once(
                narrative_v1, grade_v1, evidence_bundle, report_type_label, api_key
            )
        full_report_v2 = f"{report_title}\n\n{narrative_v2}{manifest_stamp}"
        (REPORTS_DIR / "weekly_retention_report_v2.md").write_text(full_report_v2, encoding="utf-8")

        grade_v2 = grade_report(full_report_v2, evidence_bundle)
        audit_log["attempt_2"] = {
            "passed": grade_v2["passed"],
            "summary": grade_v2["summary"],
            "failures": grade_v2["failures"],
            "uncovered_sentences": grade_v2["coverage"]["uncovered_sentences"],
        }
        audit_log["final_passed"] = grade_v2["passed"]
        final_report_md = full_report_v2
        final_grade = grade_v2

    CLAIMS_JSON_PATH.write_text(
        json.dumps({"claims": final_grade["claims"]}, indent=2), encoding="utf-8"
    )
    CLAIM_GRADE_PATH.write_text(
        json.dumps({**audit_log, "final_verification": final_grade}, indent=2),
        encoding="utf-8",
    )

    if not final_grade["passed"]:
        gate_msg = (
            "# CLAIM VERIFICATION GATE FAILURE — REPORT BLOCKED\n\n"
            f"Report failed deterministic claim verification after revision pass: "
            f"`{json.dumps(final_grade['failures'])}`\n"
        )
        REPORT_MD_PATH.write_text(gate_msg, encoding="utf-8")
        return {"status": "BLOCKED_BY_CLAIM_GATE", "passed": False, "audit": audit_log}

    REPORT_MD_PATH.write_text(final_report_md, encoding="utf-8")
    print(f"Verified report written to {REPORT_MD_PATH}")
    return {"status": "PUBLISHED", "passed": True, "audit": audit_log}


if __name__ == "__main__":
    generate_weekly_report()