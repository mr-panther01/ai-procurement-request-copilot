from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from src.solution import handle_request

ROOT = Path(__file__).resolve().parent
REQUESTS = json.loads((ROOT / "data" / "requests.json").read_text(encoding="utf-8"))
BY_ID = {request["request_id"]: request for request in REQUESTS}

st.set_page_config(page_title="Procurement Copilot", layout="wide")
st.title("AI Procurement Request Copilot")
st.caption("Evidence-backed guidance for software requests. Recommendations are advisory; people retain approval authority.")

with st.sidebar:
    st.header("Analyze a request")
    request_id = st.selectbox(
        "Purchase request",
        list(BY_ID),
        format_func=lambda rid: f"{rid} | {BY_ID[rid]['product_name']}",
    )
    architecture = st.radio(
        "Review architecture",
        ["single", "staged"],
        format_func=lambda value: "Single-agent baseline" if value == "single" else "Staged review (2 agents)",
    )
    run_analysis = st.button("Analyze request", type="primary", use_container_width=True)
    st.caption("No purchase, approval, or budget change can be made by this copilot.")

request = BY_ID[request_id]
details, result_column = st.columns([0.9, 1.1], gap="large")

with details:
    st.subheader("Request details")
    st.markdown(f"### {request['product_name']}")
    st.write(request["business_justification"] or "No business justification provided.")
    fields_left, fields_right = st.columns(2)
    fields_left.metric("Vendor", request["vendor_name"])
    fields_right.metric("Annual cost", "Not provided" if request["annual_cost_usd"] is None else f"${request['annual_cost_usd']:,.0f}")
    fields_left.metric("Users / licenses", request["user_count"] or "Not provided")
    fields_right.metric("Data access", request["data_access_level"] or "Not provided")
    st.caption(
        f"Category: {request.get('category') or 'Not provided'} | "
        f"Integrations: {', '.join(request.get('requested_integrations') or []) or 'None specified'}"
    )

with result_column:
    st.subheader("Recommendation")
    if run_analysis:
        try:
            decision = handle_request(request_id, architecture=architecture)
        except (KeyError, ValueError, RuntimeError) as exc:
            st.error(f"Analysis could not be completed: {exc}")
        else:
            st.markdown(f"#### {decision.recommendation}")
            st.info(decision.next_step)
            approval_col, risk_col = st.columns(2)
            with approval_col:
                st.markdown("**Approvals required**")
                if decision.required_approvals:
                    for approval in decision.required_approvals:
                        st.markdown(f"- {approval}")
                else:
                    st.write("None identified by the current rules.")
            with risk_col:
                st.markdown("**Risk flags**")
                if decision.risk_flags:
                    for flag in decision.risk_flags:
                        st.markdown(f"- `{flag}`")
                else:
                    st.write("No additional risk flags identified.")
            st.markdown("**Missing information**")
            if decision.missing_information:
                for item in decision.missing_information:
                    st.markdown(f"- {item}")
            else:
                st.write("No required information is missing.")
            with st.expander(f"Evidence ({len(decision.evidence)} items)", expanded=True):
                for item in decision.evidence:
                    st.markdown(f"**{item.source}** - {item.finding}")
                    if item.reference:
                        st.caption(f"Source: {item.reference}")
            if decision.telemetry:
                st.caption(
                    f"Run telemetry · {decision.telemetry.tool_calls} tool calls · "
                    f"{decision.telemetry.llm_calls} LLM calls · "
                    f"Tools: {', '.join(decision.telemetry.tool_names)}"
                )
    else:
        st.info("Select an architecture and analyze the request to see sourced evidence and the next action.")

st.divider()
st.caption(
    "Policy snapshot: 2026-09-30. Request and vendor text is treated as untrusted data. "
    "Security, Privacy, Legal, Finance, and business approvals remain human decisions."
)
