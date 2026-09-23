# Deterministic measurement contract

## State functions

Choose only the functions needed by the task.

- `BIN`: one indivisible observable condition; state is 0 or 1.
- `RATIO`: satisfied explicitly numbered conditions divided by applicable conditions. If the applicable-condition denominator can be zero, define the resulting state as `empty_set_value` (`0` or `1`) with a non-empty `empty_set_reason`; if the denominator is provably fixed and nonzero, no empty-set policy is needed.
- `COUNT`: `min(valid item count / justified target, 1)`; define a valid item and cite the target's source.
- `CLAIM-RATIO`: supported/correct audited claims divided by applicable audited claims; define claim segmentation and the complete audit universe or a justified deterministic sample. Store the no-applicable-claim result separately as `empty_set_value` (`0` or `1`) plus a non-empty `empty_set_reason`; do not infer it from prose keywords.

Use `REQnn` for requirements and `Knn` for verified anchors; reserve `Ann` for accuracy/fidelity score atoms. A mandatory requirement is covered only when a scored state rule directly observes it, not merely when its ID appears in metadata.

When the task provides a natural denominator, score the satisfied share directly. Do not replace it with an invented binary threshold such as “four of five passes.” Severe caps are reserved for material, conclusion-changing fabrication, systematic error, or contradiction—not one ordinary error or a URL that later becomes unavailable.

Mandatory omissions remain applicable and score zero. Conditional N/A requires an explicit precondition and evidence that it is false.

## Evidence and anchors

An objective anchor is valid only when the authoring record contains its source locator, derivation method, result, unit/precision, and verification result. Important formulas, transformations, and mappings should be independently cross-checked when practical.

For open tasks, audit all conclusion-changing facts, named cases, key numbers, dates, rankings, and claims used to justify recommendations. Any sampling of remaining claims must be deterministic and documented, but a sampling rule is not mandatory when all claims can be checked.

For allowed-variation atoms, determinism comes from observable relationships rather than agreement with a conclusion. Define the claim or decision being evaluated, the evidence it relies on, the minimum support relationship, and the explicit invalid boundary. Two different conclusions supported by different valid evidence may both satisfy the same atom.

## Threshold provenance

Every hard count, percentage, coverage category, or cap must come from an explicit task requirement, a natural denominator in the frozen source, or a documented benchmark-design decision necessary to distinguish performance levels. The third category must be labeled as a benchmark convention and must not masquerade as a user requirement. If no defensible basis exists, remove the threshold.

## Operational language

Replace impression terms with observations. For example, “clear” can mean identifiable by a heading, label, unique phrase, or specified locator; “usable” can mean opens, renders, and exposes required content without clipping/corruption. Definitions are task-local and do not create new obligations.

## Non-overlap and precedence

Each concrete defect has one primary scoring home inside a metric. Cross-metric effects must measure genuinely different constructs and be declared. Caps only lower results and require a concrete trigger.

## Semantic stability

Compute a semantic fingerprint from the structured authoring record: judgment profile, objective-verifiable checks, allowed-variation boundaries, requirement IDs, anchor IDs/values, hard thresholds with provenance, atom layers/IDs/ownership, weights, state functions, formulas, linked source IDs, and error triggers. Do not hash free-form Rubric prose as meaning; equivalent wording may differ. Independent drafts from the same frozen inputs should match on this fingerprint. If they do not, resolve the source ambiguity or design decision; do not average the drafts.
