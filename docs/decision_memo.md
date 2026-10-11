# Architecture decision memo

## Decision

Ship the single-agent workflow for this MVP.

## Evidence

Both implementations passed all six public cases using the same local mock vendor-risk API. Each averaged five tool calls and zero LLM calls in the default, credential-free setup. The single workflow averaged 66.3 ms and staged averaged 72.6 ms in this local run. The evaluator alternates architecture order per case to reduce cold-start bias; this six-case sample is still too small to claim a stable latency difference. The generated, reproducible measurements are in `evals/architecture_comparison.csv`.

| Metric | Single | Staged |
|---|---:|---:|
| Public cases passing minimum checks | 6/6 | 6/6 |
| Average latency | 66.3 ms | 72.6 ms |
| Average LLM calls | 0 | 0 |
| Average tool calls | 5 | 5 |
| Public-check policy failures | 0 | 0 |

## Trade-offs and rationale

The staged version has explicit Evidence Analyst and Policy Reviewer boundaries, which may be helpful if future teams own evidence gathering and policy validation separately. In the current implementation, both stages use the same bounded data retrieval and deterministic rules; staging adds no measured correctness benefit or new authority. The single workflow is simpler to maintain and audit, while the same code-based controls still enforce budget thresholds, approvals, missing-information escalation, vendor-risk uncertainty, and human review.

The optional OpenAI-compatible calls were disabled for this evaluation, so the reported results do not compare model quality or provider latency. If enabled, single makes one non-authoritative context-summary call and staged makes two; neither can change structured policy findings.

## Risks and next validation

The public set is small and synthetic. Before production, test hidden and adversarial requests, repeat latency tests under load, validate real source freshness and identity controls, review model data handling, and audit every policy boundary with Procurement, Finance, Security, Privacy, and Legal. No result authorizes purchasing or substitutes for human approval.
