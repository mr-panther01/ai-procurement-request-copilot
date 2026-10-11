from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date

import requests

from src.contracts import Architecture, EvidenceItem, ProcurementDecision, RunTelemetry
from src.data_access import (
    get_request,
    load_budgets,
    load_employees,
    load_purchase_history,
    load_software_catalog,
    load_vendors,
)
from src.llm import summarize_request, summarize_review
from src.telemetry import RunTelemetryCounter
from src.vendor_client import get_vendor_risk

REFERENCE_DATE = date(2026, 9, 30)
INJECTION_PATTERN = re.compile(
    r"\b(ignore|bypass|override|disregard)\b.{0,80}\b(policy|rules|controls|approval|instructions)\b"
    r"|\b(treat this request as|approve it immediately|ignore all procurement rules|"
    r"ignore previous instructions|skip approvals|system prompt|reveal secrets)\b",
    re.IGNORECASE,
)
SENSITIVE_DATA_MARKERS = (
    "source_code",
    "production",
    "confidential",
    "employee_pii",
    "customer_pii",
    "credentials",
    "secrets",
    "cloud_account",
    "git_repository",
)


@dataclass
class EvidenceBundle:
    request: dict
    employee: dict
    budget: dict | None
    catalog_matches: list[dict]
    purchase_history: list[dict]
    vendor: dict | None
    vendor_risk: dict | None
    vendor_risk_error: str | None


def _records(frame) -> list[dict]:
    return frame.fillna("").to_dict(orient="records")


def _request_context(request_id: str, counter: RunTelemetryCounter) -> tuple[dict, dict]:
    counter.record_tool_call("request_context")
    request = get_request(request_id)
    employees = {
        row["employee_id"]: row
        for row in _records(load_employees())
    }
    employee = employees.get(request.get("requester_id"))
    if employee is None:
        raise ValueError(f"Requester {request.get('requester_id')!r} is not in the employee directory")
    return request, employee


def _budget_check(department: str, counter: RunTelemetryCounter) -> dict | None:
    counter.record_tool_call("deterministic_budget_policy")
    for row in _records(load_budgets()):
        if row["department"].casefold() == department.casefold():
            return row
    return None


def _catalog_search(request: dict, counter: RunTelemetryCounter) -> list[dict]:
    counter.record_tool_call("software_catalog_search")
    product = str(request.get("product_name") or "").casefold()
    vendor = str(request.get("vendor_name") or "").casefold()
    category = str(request.get("category") or "").casefold()
    matches = []
    for row in _records(load_software_catalog()):
        if not str(row.get("status") or "").casefold().startswith("approved"):
            continue
        same_product = bool(product and product in str(row.get("product_name") or "").casefold())
        same_vendor = bool(vendor and vendor == str(row.get("vendor_name") or "").casefold())
        same_category = bool(category and category == str(row.get("category") or "").casefold())
        if same_product or same_vendor or same_category:
            matches.append(row)
    return matches


def _vendor_check(vendor_name: str, counter: RunTelemetryCounter) -> tuple[dict | None, dict | None, str | None]:
    counter.record_tool_call("vendor_registry_and_risk_api")
    vendor = next(
        (
            row
            for row in _records(load_vendors())
            if str(row.get("vendor_name") or "").casefold() == vendor_name.casefold()
        ),
        None,
    )
    try:
        risk = get_vendor_risk(vendor_name)
    except (requests.RequestException, ValueError) as exc:
        return vendor, None, f"{type(exc).__name__}: {exc}"
    return vendor, risk, None


def _purchase_history_search(vendor_name: str, counter: RunTelemetryCounter) -> list[dict]:
    counter.record_tool_call("purchase_history_search")
    return [
        row
        for row in _records(load_purchase_history())
        if str(row.get("vendor_name") or "").casefold() == vendor_name.casefold()
    ]


def _gather_evidence(request_id: str, counter: RunTelemetryCounter) -> EvidenceBundle:
    request, employee = _request_context(request_id, counter)
    budget = _budget_check(str(employee["department"]), counter)
    matches = _catalog_search(request, counter)
    purchases = _purchase_history_search(str(request.get("vendor_name") or ""), counter)
    vendor, vendor_risk, vendor_risk_error = _vendor_check(
        str(request.get("vendor_name") or ""), counter
    )
    return EvidenceBundle(
        request=request,
        employee=employee,
        budget=budget,
        catalog_matches=matches,
        purchase_history=purchases,
        vendor=vendor,
        vendor_risk=vendor_risk,
        vendor_risk_error=vendor_risk_error,
    )


def _evidence_analyst(request_id: str, counter: RunTelemetryCounter) -> EvidenceBundle:
    return _gather_evidence(request_id, counter)


def _annual_cost(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


def _license_count(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        count = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(count) or count <= 0 or not count.is_integer():
        return None
    return int(count)


def _missing_information(request: dict, employee: dict) -> list[str]:
    missing: list[str] = []
    if not employee.get("name") or not employee.get("department"):
        missing.append("requester or department")
    if not request.get("product_name") or not request.get("vendor_name"):
        missing.append("product or vendor")
    if _annual_cost(request.get("annual_cost_usd")) is None:
        missing.append("annual cost")
    if _license_count(request.get("user_count")) is None:
        missing.append("number of users or licenses")
    if not str(request.get("business_justification") or "").strip():
        missing.append("business purpose")
    data_access = str(request.get("data_access_level") or "").strip().casefold()
    if not data_access or data_access in {"unknown", "unspecified", "n/a"}:
        missing.append("intended data access level")
    if request.get("requested_integrations") is None:
        missing.append("required integrations")
    return missing


def _approval_thresholds(cost: int | float | None) -> list[str]:
    if cost is None:
        return []
    amount = float(cost)
    if amount <= 1_000:
        return ["Manager"]
    if amount <= 10_000:
        return ["Department Head", "Procurement"]
    if amount <= 25_000:
        return ["Department Head", "Finance", "Procurement"]
    return ["Department Head", "Finance", "CFO", "Procurement"]


def _review_age_days(review_date: str | None) -> int | None:
    if not review_date:
        return None
    try:
        return (REFERENCE_DATE - date.fromisoformat(str(review_date))).days
    except ValueError:
        return None


def _is_sensitive_data(data_access: str) -> bool:
    normalized = data_access.casefold().replace(" ", "_").replace("-", "_")
    return any(marker in normalized for marker in SENSITIVE_DATA_MARKERS)


def _security_status_family(status: str) -> str:
    normalized = status.strip().casefold().replace(" ", "_")
    if normalized in {"approved", "current", "complete", "completed"}:
        return "current"
    if normalized == "expired":
        return "expired"
    if normalized in {"", "unknown", "pending", "not_completed", "incomplete"}:
        return "pending"
    return normalized


def _decision(bundle: EvidenceBundle, architecture: Architecture, counter: RunTelemetryCounter) -> ProcurementDecision:
    request = bundle.request
    employee = bundle.employee
    vendor = bundle.vendor or {}
    risk = bundle.vendor_risk or {}
    missing = _missing_information(request, employee)
    flags: list[str] = []
    cost = _annual_cost(request.get("annual_cost_usd"))
    user_count = _license_count(request.get("user_count"))
    approvals = _approval_thresholds(cost)
    evidence: list[EvidenceItem] = []

    evidence.append(
        EvidenceItem(
            source="Procurement policy",
            finding="Policy version 2026.09 applied with 2026-09-30 as the reference date; recommendation is advisory and final approval remains human.",
            reference="procurement_policy.md §§ 4-11",
        )
    )
    evidence.append(
        EvidenceItem(
            source="Request and employee directory",
            finding=(
                f"{employee.get('name')} ({employee.get('department')}) requested "
                f"{request.get('product_name')} from {request.get('vendor_name')} "
                f"for {user_count or 'unspecified'} users; stated data access: "
                f"{request.get('data_access_level') or 'unspecified'}."
            ),
            reference=request["request_id"],
        )
    )

    if bundle.budget:
        available = float(bundle.budget["available_usd"])
        evidence.append(
            EvidenceItem(
                source="Deterministic budget check",
                finding=(
                    f"{employee.get('department')} has ${available:,.0f} available; "
                    + (
                        f"the ${cost:,.0f} annual request is within budget."
                        if cost is not None and cost <= available
                        else (
                            f"the ${cost:,.0f} annual request exceeds available budget."
                            if cost is not None
                            else "the request cost is missing or invalid, so affordability cannot be determined."
                        )
                    )
                ),
                reference="department_budgets.csv",
            )
        )
        if cost is not None and cost > available:
            flags.append("budget_insufficient")
            if "Finance" not in approvals:
                approvals.append("Finance")
    else:
        missing.append("department software budget")
        flags.append("missing_information")

    if bundle.catalog_matches:
        flags.append("existing_tool_overlap")
        summarized = "; ".join(
            f"{row['product_name']} ({row['vendor_name']}, {row['scope']}, {row['licensed_seats']} seats)"
            for row in bundle.catalog_matches[:4]
        )
        evidence.append(
            EvidenceItem(
                source="Approved software catalog",
                finding=f"Potential existing alternatives in the same product, vendor, or category: {summarized}.",
                reference="software_catalog.csv",
            )
        )
    else:
        evidence.append(
            EvidenceItem(
                source="Approved software catalog",
                finding="No approved product with the same vendor, product, or category was found.",
                reference="software_catalog.csv",
            )
        )

    if bundle.purchase_history:
        purchase_summary = "; ".join(
            f"{row['product_name']} ({row['status']}, {row['purchase_date']}, ${float(row['annual_amount_usd']):,.0f}/year)"
            for row in bundle.purchase_history[:4]
        )
        finding = f"Previous purchases from this vendor: {purchase_summary}."
    else:
        finding = "No purchase-history records were found for this vendor."
    evidence.append(
        EvidenceItem(
            source="Purchase history",
            finding=finding,
            reference="purchase_history.csv",
        )
    )

    registry_status = str(vendor.get("security_status") or "").strip().casefold()
    risk_status = str(risk.get("security_review_status") or "").strip().casefold()
    review_date = risk.get("last_review_date") or vendor.get("security_review_date")
    age_days = _review_age_days(review_date)
    stale = age_days is None or age_days > 365
    future_review = age_days is not None and age_days < 0
    if bundle.vendor_risk_error:
        flags.extend(["vendor_risk_unavailable", "security_review_required"])
        evidence.append(
            EvidenceItem(
                source="Vendor-risk service",
                finding=f"Could not verify current vendor risk status: {bundle.vendor_risk_error}",
                reference="vendor-risk API",
            )
        )
        missing.append("current vendor security assessment")
    else:
        evidence.append(
            EvidenceItem(
                source="Vendor registry and vendor-risk service",
                finding=(
                    f"Registry security status: {vendor.get('security_status') or 'not recorded'}; "
                    f"service status: {risk.get('security_review_status') or 'not recorded'}; "
                    f"last review: {review_date or 'not recorded'} "
                    f"(reference date 2026-09-30; age: {age_days if age_days is not None else 'unknown'} days)."
                ),
                reference="vendors.csv + vendor-risk API",
            )
        )
        if (
            registry_status
            and risk_status
            and _security_status_family(registry_status) != _security_status_family(risk_status)
        ):
            flags.append("conflicting_vendor_evidence")
            flags.append("security_review_required")
        if future_review:
            flags.extend(["conflicting_vendor_evidence", "security_review_required"])
        if stale:
            flags.extend(["vendor_review_expired", "security_review_required"])
        if risk_status not in {"approved", "current", "complete", "completed"}:
            flags.append("security_review_required")
    if str(vendor.get("procurement_status") or "").casefold() == "new":
        if cost is not None and cost >= 10_000:
            flags.append("legal_review_required")
    if str(vendor.get("legal_terms_status") or "").casefold() not in {"approved", "standard"}:
        flags.append("legal_review_required")

    data_access = str(request.get("data_access_level") or "")
    integration_text = " ".join(str(value) for value in request.get("requested_integrations") or [])
    sensitive_access = _is_sensitive_data(data_access) or _is_sensitive_data(integration_text)
    if sensitive_access:
        flags.append("security_review_required")
    data_access_normalized = data_access.casefold().replace(" ", "_").replace("-", "_")
    is_pii = "customer_pii" in data_access_normalized or "employee_pii" in data_access_normalized
    outside_region = bool(risk.get("stores_data_outside_region"))
    if is_pii or outside_region:
        flags.append("privacy_review_required")
    if outside_region and data_access_normalized not in {"", "none", "unknown"}:
        flags.append("legal_review_required")

    if "security_review_required" in flags:
        approvals.append("Security")
    if "privacy_review_required" in flags:
        approvals.append("Privacy")
    if "legal_review_required" in flags:
        approvals.append("Legal")

    justification = str(request.get("business_justification") or "")
    injection_detected = bool(INJECTION_PATTERN.search(justification))
    if injection_detected:
        flags.append("prompt_injection_detected")
        evidence.append(
            EvidenceItem(
                source="Untrusted-content guard",
                finding="Request text contains an instruction-like attempt to bypass procurement controls; it was treated as untrusted business data.",
                reference=request["request_id"],
            )
        )
    if missing:
        flags.append("missing_information")

    approvals = list(dict.fromkeys(approvals))
    flags = list(dict.fromkeys(flags))
    if missing:
        recommendation = "Request clarification before approval review"
        next_step = "Ask the requester to provide: " + ", ".join(missing) + "."
    elif flags:
        recommendation = "Hold for required human review"
        next_step = "Route to " + ", ".join(approvals or ["Procurement"]) + " for review; do not place an order."
    elif "existing_tool_overlap" in flags:
        recommendation = "Review existing alternatives before creating a new purchase"
        next_step = "Ask the business owner to document the capability gap and review existing licenses."
    else:
        recommendation = "Route for standard approvals"
        next_step = "Submit the request to the listed approvers; no purchase is authorized by this recommendation."

    if "existing_tool_overlap" in flags and not missing:
        recommendation = "Review existing alternatives and complete required human approvals"
        next_step += " Also document why the existing catalog options do not meet the need."

    if os.getenv("ENABLE_LLM", "").strip().casefold() in {"1", "true", "yes"}:
        model_stages = [("Request interpretation", summarize_request)]
        if architecture == "staged":
            model_stages.append(
                (
                    "Review summary",
                    lambda current_request: summarize_review(
                        current_request,
                        {
                            "recommendation": recommendation,
                            "required_approvals": approvals,
                            "risk_flags": flags,
                            "missing_information": missing,
                        },
                    ),
                )
            )
        for stage_name, model_call in model_stages:
            counter.record_llm_call()
            try:
                summary = model_call(request)
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                evidence.append(
                    EvidenceItem(
                        source=f"Optional LLM · {stage_name}",
                        finding=f"Model output unavailable ({type(exc).__name__}: {exc}); deterministic policy findings remain authoritative.",
                        reference="optional OpenAI-compatible chat completions endpoint",
                    )
                )
            else:
                evidence.append(
                    EvidenceItem(
                        source=f"Optional LLM · {stage_name} (non-authoritative)",
                        finding=summary,
                        reference="request context only; no approval decision",
                    )
                )

    if architecture == "staged":
        evidence.append(
            EvidenceItem(
                source="Staged review",
                finding="Evidence analyst gathered records; policy reviewer independently applied deterministic rules before presenting this recommendation.",
                reference="Evidence Analyst -> Policy Reviewer",
            )
        )

    return ProcurementDecision(
        request_id=request["request_id"],
        recommendation=recommendation,
        evidence=evidence,
        required_approvals=approvals,
        missing_information=missing,
        risk_flags=flags,
        next_step=next_step,
        human_review_required=True,
        telemetry=RunTelemetry(
            llm_calls=counter.llm_calls,
            tool_calls=counter.tool_calls,
            tool_names=counter.tool_names,
        ),
    )


def _policy_reviewer(
    bundle: EvidenceBundle, counter: RunTelemetryCounter
) -> ProcurementDecision:
    return _decision(bundle, "staged", counter)


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Run the single-agent baseline or a lightweight staged review."""
    if architecture not in {"single", "staged"}:
        raise ValueError(f"Unsupported architecture: {architecture!r}")
    counter = RunTelemetryCounter()

    if architecture == "single":
        bundle = _gather_evidence(request_id, counter)
        return _decision(bundle, architecture, counter)

    # Stage 1 (Evidence Analyst) gathers the same bounded, auditable tool results.
    bundle = _evidence_analyst(request_id, counter)
    # Stage 2 (Policy Reviewer) applies deterministic policy and never trusts stage-1 text as authority.
    return _policy_reviewer(bundle, counter)
