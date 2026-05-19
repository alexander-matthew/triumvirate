# Constitution ratification record

The triumvirate constitution at `templates/constitution.md` was ratified
by unanimous triumvirate consensus on **2026-05-18**.

The ratification process itself was the first invocation of the
constitution's tier-3 rule (unanimous APPROVE from all three CLIs), so
the process and its outcome are recorded here as the foundational
precedent for all future amendments.

## Verdicts

### Round 1 — Initial draft

| CLI    | Verdict           | Summary |
|--------|-------------------|---------|
| claude | APPROVE (author)  | Drafted the document; implicit APPROVE by authorship. |
| codex  | REQUEST_CHANGES   | Five substantive blockers — Tier 2 mislabelled as "simple majority"; hard-limits language too weak; `[guards].protected_paths` setting not itself protected; `[project].trusted_authors` underspecified; tier-3 detection relied solely on config (risk of misclassification). |
| gemini | APPROVE           | Found the tiers clearly demarcated, the values operationalised, kill-switches inviolable. |

Round 1 did not reach the unanimous threshold. The draft was amended.

### Round 2 — Amended draft

| CLI    | Verdict           | Notes |
|--------|-------------------|-------|
| claude | APPROVE (author)  | Applied all five of codex's blockers. |
| codex  | APPROVE           | All six checklist items PASS. Flagged "implementation fidelity" — the merge gate must actually enforce what the constitution promises. |
| gemini | APPROVE           | All six checklist items PASS. Suggested clarifying that `merge_gate.py` is treated as a tier-3 mechanism despite its location-based tier-2 classification. |

Round 2 reached unanimous APPROVE. **Constitution ratified.**

## Follow-up items recorded at ratification

These are explicitly *not* part of the ratified text. They are queued
for the synthesis phase to propose as future amendments per the
tier-3 amendment process the constitution defines. Author-side
patching after ratification would defeat the purpose of the threshold.

1. **Merge-gate enforcement implementation.** The constitution describes
   defence-in-depth detection of tier-3 surfaces, human-author
   verification on hard-limit settings, and rejection of
   `protected_paths` edits ahead of tier logic. The code that enforces
   these is not yet written; it is the next implementation pass after
   ratification.

2. **Functional-rule clarification.** Gemini noted that
   `src/agent_loop/lib/persona.py` (the constitution loader) and
   `src/agent_loop/phases/merge_gate.py` (the tier gate itself) are
   "mechanisms that judge other changes" and should be tier-3 in any
   conflict with the general tier-2 classification of phase / lib
   files. A future amendment proposal should make this explicit in the
   constitution rather than relying on persona interpretation.

## Verbatim verdict files

The raw outputs of each ratification call are preserved in `/tmp/` on
the host that ran the ratification — those files are ephemeral. The
authoritative record is this document plus the constitution at the
SHA committed alongside it.
