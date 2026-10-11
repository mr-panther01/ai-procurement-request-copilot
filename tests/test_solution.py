from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.contracts import ProcurementDecision
from src.solution import _annual_cost, _approval_thresholds, _license_count, handle_request

ROOT = Path(__file__).resolve().parents[1]
RISK_DATA = json.loads((ROOT / "data" / "vendor_risk.json").read_text(encoding="utf-8"))


class ProcurementSolutionTests(unittest.TestCase):
    def test_numeric_inputs_and_approval_boundaries(self):
        self.assertIsNone(_annual_cost(-1))
        self.assertIsNone(_annual_cost("not a price"))
        self.assertIsNone(_license_count(0))
        self.assertIsNone(_license_count(1.5))
        self.assertEqual(_approval_thresholds(1_000), ["Manager"])
        self.assertEqual(_approval_thresholds(10_000), ["Department Head", "Procurement"])
        self.assertEqual(
            _approval_thresholds(10_000.01),
            ["Department Head", "Finance", "Procurement"],
        )
        self.assertEqual(
            _approval_thresholds(25_000.01),
            ["Department Head", "Finance", "CFO", "Procurement"],
        )

    def risk_result(self, vendor_name: str) -> dict:
        record = RISK_DATA[vendor_name]
        if record.get("force_error"):
            raise requests.HTTPError(record.get("error_message", "service unavailable"))
        return {"vendor_name": vendor_name, **record}

    def run_decision(self, request_id: str, architecture: str = "single") -> ProcurementDecision:
        with patch.dict(os.environ, {"ENABLE_LLM": "false"}):
            with patch("src.solution.get_vendor_risk", side_effect=self.risk_result):
                return handle_request(request_id, architecture=architecture)

    def test_single_architecture_returns_contract_and_records_tools(self):
        result = self.run_decision("REQ-1001")
        self.assertIsInstance(result, ProcurementDecision)
        self.assertIn("Manager", result.required_approvals)
        self.assertEqual(result.telemetry.tool_calls, 5)
        self.assertEqual(result.telemetry.llm_calls, 0)
        self.assertTrue(result.human_review_required)

    def test_budget_privacy_and_security_reviews_are_deterministic(self):
        result = self.run_decision("REQ-1005")
        self.assertIn("budget_insufficient", result.risk_flags)
        self.assertIn("security_review_required", result.risk_flags)
        self.assertIn("privacy_review_required", result.risk_flags)
        self.assertIn("legal_review_required", result.risk_flags)
        self.assertTrue({"Finance", "Security", "Privacy", "Legal"}.issubset(result.required_approvals))

    def test_incomplete_adversarial_request_is_not_approved(self):
        result = self.run_decision("REQ-1006")
        self.assertTrue(any("cost" in item.casefold() for item in result.missing_information))
        self.assertTrue(any("user" in item.casefold() for item in result.missing_information))
        self.assertTrue(any("data" in item.casefold() for item in result.missing_information))
        self.assertIn("prompt_injection_detected", result.risk_flags)
        self.assertTrue(result.human_review_required)

    def test_vendor_service_outage_is_not_treated_as_approval(self):
        result = self.run_decision("REQ-1009")
        self.assertIn("vendor_risk_unavailable", result.risk_flags)
        self.assertIn("security_review_required", result.risk_flags)
        self.assertIn("current vendor security assessment", result.missing_information)

    def test_expired_and_conflicting_vendor_assessment_escalates(self):
        result = self.run_decision("REQ-1007")
        self.assertIn("vendor_review_expired", result.risk_flags)
        self.assertIn("conflicting_vendor_evidence", result.risk_flags)
        self.assertIn("Security", result.required_approvals)

    def test_equivalent_incomplete_vendor_statuses_are_not_reported_as_conflicting(self):
        result = self.run_decision("REQ-1002")
        self.assertIn("security_review_required", result.risk_flags)
        self.assertNotIn("conflicting_vendor_evidence", result.risk_flags)

    def test_optional_model_summarizes_without_changing_policy_results(self):
        baseline = self.run_decision("REQ-1003")
        model_response = Mock()
        model_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"summary": "Context summary."})}}]
        }
        with patch.dict(os.environ, {"ENABLE_LLM": "true", "OPENAI_API_KEY": "test-key"}):
            with patch("src.solution.get_vendor_risk", side_effect=self.risk_result):
                with patch("src.llm.requests.post", return_value=model_response) as model_call:
                    assisted = handle_request("REQ-1003", architecture="staged")
        self.assertEqual(model_call.call_count, 2)
        self.assertEqual(assisted.telemetry.llm_calls, 2)
        self.assertEqual(assisted.required_approvals, baseline.required_approvals)
        self.assertEqual(assisted.risk_flags, baseline.risk_flags)

    def test_staged_architecture_has_a_separate_review_evidence_item(self):
        result = self.run_decision("REQ-1003", "staged")
        self.assertTrue(any(item.source == "Staged review" for item in result.evidence))
        self.assertIn("security_review_required", result.risk_flags)

    def test_unknown_request_fails_explicitly(self):
        with self.assertRaises(KeyError):
            handle_request("REQ-DOES-NOT-EXIST")


if __name__ == "__main__":
    unittest.main()
