# Free Mode Runtime Intent

Date: 2026-08-09 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`

## Input authority

Free Mode has one current typed input artifact:
`free_mode_intent_input/v1`. The external profile path is metadata only;
after intake the Registry artifact bytes are the authority. Resume reads the
frozen envelope from the persisted runtime job spec and verifies the Registry
artifact; it never silently rereads a changed external profile.

## Transport

`RuntimeJobSpec` carries `free_mode_profile` and `free_mode_idea` through
round-trip, CLI mapping, GUI queue parameters, and direct runner execution.
Profile and idea remain mutually exclusive, and a missing profile is rejected.

## ReviewIntent projection

The projection is deterministic and literal:

- `research_goal` -> `review_question`
- `focus_points` + `theory_or_variable_focus` -> `must_cover`
- `exclusions` -> `must_not_do`
- `outline_preferences` -> `preferred_organizing_logic`
- literal profile fields such as `scope`, `target_audience`,
  `desired_contribution`, `language`, `target_depth`, and `target_length`
  map only when present

`concept_relationship`, `generated_prompt`, and `writing_constraints` are
preserved in the typed Free Mode context rather than silently reinterpreted.
For idea mode, the normalized idea is mapped literally to
`ReviewIntent.review_question`; no hidden LLM conversion or inference is used.
The projection is published as `free_mode_review_intent_projection/v1` with a
Registry dependency on the typed input artifact. Changing profile semantics
changes `review_intent_hash`, Outline replay identity, and affected Outline v3
nodes; unchanged profile bytes preserve exact replay.

## Writer v3

`ReviewGenerationService` receives the registered Free Mode context through
an explicit argument. The section prompt includes it exactly once, and each
section binding carries `free_mode_input_artifact_id`,
`free_mode_input_artifact_hash`, and `free_mode_context_hash`. Changing
writing constraints invalidates Writer replay; unchanged context preserves
exact replay.

## Stage 1

Free Mode input is intentionally not injected into Stage 1. Stage 1 remains
generic canonical paper understanding, and a different Free Mode profile does
not invalidate the reusable Stage 1 summary.

## Concept Mode

`CURRENTLY DISABLED HONESTLY`. Concept Mode is disabled at all public/runtime
boundaries. The GUI and central JobRunner validator reject stale `concept`
requests with a clear message:
Concept Mode is not yet available in the current PR14 runtime.
