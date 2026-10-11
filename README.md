# AI Procurement Request Copilot

An evidence-backed internal product for reviewing software purchase requests. It gathers business and vendor evidence, applies procurement policy deterministically, and recommends a human next step. It cannot approve spend, change budgets, accept terms, or place orders.

All sample employees, vendors, products, prices, policies, and risk signals are synthetic assessment data.

## Run locally

Requires Python 3.11 or newer. From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run_local.py
```

Open the Streamlit product at <http://127.0.0.1:8501>. The launcher starts the mock vendor-risk service at <http://127.0.0.1:8001> first. No model key is needed for the deterministic workflow.

Optional LLM context summaries can be enabled by copying `.env.example` to `.env`, setting `ENABLE_LLM=true`, and supplying `OPENAI_API_KEY`. `OPENAI_BASE_URL` and `MODEL_NAME` configure an OpenAI-compatible Chat Completions endpoint. LLM text is displayed as non-authoritative context only; it cannot change policy results.

## Product workflow

1. Select a seeded request and choose the single-agent or staged review architecture.
2. The copilot loads the requester, department budget, approved catalog, vendor registry, policy, and mock vendor-risk response.
3. Deterministic checks compare budget and approval thresholds, detect potential tool overlap, enforce Security/Privacy/Legal escalation, and identify missing fields or untrusted instruction-like text.
4. The UI presents the recommendation, sourced evidence, approvals, missing information, risk flags, and next step.
5. A human reviews and decides. The recommendation is not an approval.

The structured adapter is `src/solution.py:handle_request(request_id, architecture)`, with `single` and `staged` values.

## Tools and agent designs

Tool set:
- Request and employee directory lookup.
- Approved software catalog search for exact product/vendor and category candidates.
- Purchase-history search for prior orders with the requested vendor.
- Vendor registry plus mock vendor-risk API retrieval; conflicts, expired review, and API failures are surfaced.
- Deterministic department budget and financial threshold checks.

Architecture A (single) gathers evidence and returns the deterministic policy result in one workflow. Architecture B (staged) separates evidence gathering (Evidence Analyst) from deterministic policy review (Policy Reviewer). Both use the same tools, rules, and public cases. If explicitly enabled, the single architecture makes one optional model call and staged makes two calls, only for summaries.

See [docs/architecture.md](docs/architecture.md) for the workflow diagram, boundaries, and assumptions.

## Evaluation

Run all public cases against both architectures with an ephemeral local mock API:

```powershell
python evals/run_comparison.py
```

The script writes `evals/results_single.csv`, `evals/results_staged.csv`, `evals/architecture_comparison.csv`, and `evals/architecture_comparison.md`. Latency is machine-dependent. Set `ENABLE_LLM=true` and configure credentials only if you intend to include external model calls.

Run the focused unit tests and starter-pack preflight:

```powershell
python -m unittest discover -s tests -v
python verify_setup.py
```

To run the original per-architecture harness manually, start `python run_local.py` in another terminal, then run `python evals/run_public_evals.py --architecture single` and `--architecture staged`.

## Architecture decision

Both variants passed all six public cases. This run averaged 66.3 ms and five tool calls for single, versus 72.6 ms and five tool calls for staged; both used zero LLM calls. The evaluator alternates architecture order per case, but this single local run is still too small to establish a stable latency difference.

| Metric | Single | Staged |
|---|---:|---:|
| Public cases passed | 6/6 | 6/6 |
| Average latency (ms) | 66.3 | 72.6 |
| Average LLM calls | 0 | 0 |
| Average tool calls | 5 | 5 |

Ship the single workflow for this MVP: staging showed no correctness gain, and this run does not show a meaningful latency advantage for the extra orchestration. Results are in `evals/architecture_comparison.csv`; the rationale and limitations are in [docs/decision_memo.md](docs/decision_memo.md).

## Limitations

- The dataset and endpoint are synthetic; production connectors, access controls, audit logging, and source freshness must be designed and validated.
- Catalog matching deliberately surfaces same-category products for a human, rather than automatically rejecting a request.
- An optional LLM summary is not a policy engine and should be reviewed before any production use.
- Current evaluation cases are public and limited; additional hidden and adversarial scenarios are necessary before deployment.

## Secrets

Keep `.env` and all provider credentials out of version control. `.env.example` contains placeholders only.
