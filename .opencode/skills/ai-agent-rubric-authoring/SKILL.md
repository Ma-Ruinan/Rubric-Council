---
name: ai-agent-rubric-authoring
description: Design, revise, or audit task-specific AI Agent benchmark rubrics by first solving and verifying the task enough to establish source-backed anchors, then converting those anchors and task requirements into deterministic, traceable scoring rules. Do not use to score participant deliveries or generate evaluation reports.
---

# AI Agent Rubric Authoring

A rubric is not a reference answer. It is an executable judging contract: two evaluators using the same delivery evidence should reach the same atom states and scores.

## Authority and purpose

Use this order of authority:

1. the user's current requirements;
2. the problem description and supplied source artifacts;
3. verified facts tied to the required version or time cutoff;
4. the task's authoring record and anchor evidence;
5. this Skill's defaults and examples.

This Skill is guidance, not an authority that may override the task. Never force a task into a generic template merely to satisfy this Skill.

## Non-negotiable principles

1. **Solve to audit, not to answer.** Before writing criteria, work through the task far enough—often more than once—to identify correct anchors, legitimate answer variation, required process evidence, and likely failure modes. Record verifiable results, not hidden chain-of-thought.
2. **Make the ruler grow from the task.** Classify the task and derive each hard criterion from the problem, frozen source, versioned code, or a documented evaluation necessity. Do not invent deliverables, research dimensions, region counts, fact counts, file formats, or thresholds because they look generally useful.
3. **Recompute objective anchors.** For objective or frozen-data tasks, derive exact rows, columns, values, formulas, sequences, mappings, units, names, and tolerances from the real source. Record the source locator, method, result, and an independent cross-check when practical. Never use an unverified model guess as a gold value.
4. **Use two layers for subjective work.** Separate (a) objectively verifiable requirements, facts, source quality, and evidence locators from (b) allowed variation in viewpoint, structure, method, or conclusion. The second layer judges whether evidence and reasoning support the chosen path and whether it crosses an explicit invalid boundary; it never rewards agreement with the author's preferred answer. Do not turn one plausible outline or conclusion into the only acceptable answer.
5. **Use computable states.** Every scored atom needs an ID, positive weight, observable condition, evidence locator, and one explicit state rule. Use only the state functions needed by that task: `BIN`, `RATIO`, `COUNT`, or `CLAIM-RATIO`. A rubric need not use all four.
6. **Eliminate impression scoring.** Words such as “reasonable”, “good”, “clear”, “important”, “representative”, “appropriate”, or “in-depth” are not scoring rules unless immediately replaced by observable conditions.
7. **Keep evidence traceable.** A satisfied state must cite a file/page/sheet/cell/slide/section/code symbol/URL or another stable locator. An absence finding must state what was inspected.
8. **Keep completion and quality distinct.** Do not deduct the same concrete defect twice inside one metric. A downstream reporting shell may require completion plus standard quality dimensions, but it must not create artificial task obligations.
9. **Respect time and version.** Live claims are judged at the participant's execution/cutoff time; software against the named version; frozen files against their supplied content. Later information cannot penalize an earlier valid answer.
10. **Do not inspect participant deliveries while authoring.** Historical deliveries, winners, prior scores, and evaluation conclusions must not shape the rubric.
11. **Reviewer validates; reviewer does not re-author.** Review checks task fidelity, anchor provenance, scope, computability, evidence, non-overlap, and whether requested fixes were applied. It must not add a preferred outline, new topic, or arbitrary threshold.
12. **Stability is semantic, not verbal.** Repeated authoring with the same task, source snapshot, model configuration, and authoring record should preserve requirements, anchors, atom ownership, thresholds, and formulas. Wording may differ.
13. **Keep namespaces and traceability unambiguous.** Use `REQnn` for requirements, `Knn` for verified anchors, and reserve `Ann` for accuracy/fidelity scoring atoms. Every mandatory requirement must be directly observable in at least one scored state rule; a reference ID alone is not coverage.
14. **Make ratios total functions.** `CLAIM-RATIO` defines claim segmentation, the complete audit universe or justified deterministic sample, and always records its empty-set result in structured `empty_set_value` and `empty_set_reason` fields. A `RATIO` whose natural denominator can be zero must also record those fields; a `RATIO` with a provably nonzero fixed denominator may omit them. Prefer natural denominators to invented pass thresholds.
15. **Reserve caps for material failures.** A severe cap requires a sourced, conclusion-changing fabrication, systematic error, or contradiction. One ordinary mistake or a link that later becomes unavailable is not enough by itself.
16. **Keep the final rubric portable.** It may name real dataset files and external sources, but never manifests, authoring records, specs, reviews, schema retries, run directories, or other build artifacts.
17. **Do not downgrade proven score-changing defects.** A classification gate promotes a suggestion only when the existing rubric supports a concrete, reproducible counterexample showing an incorrect atom state, score, cap, pass/fail result, or evaluator disagreement. Hypothetical risk, partial overlap, wording preference, or “could be clearer” remains advisory unless that scoring consequence is demonstrated.
18. **Structure and verify live sources.** Record each external source separately with publisher, title, date/version, complete HTTPS URL, supported claim scope, verification status, and evidence. Never guess a URL. An inaccessible source needs a stable fallback and cannot solely support a key anchor.
13. **Audit conservatively.** Report a blocking defect only when evidence shows that the rubric cannot reliably judge the task. Treat heuristic matches, stylistic preferences, and items needing human confirmation as non-blocking advisories. A usable rubric needs no revision merely to satisfy an auditor. Auditing is read-only unless the user separately authorizes edits.
14. **Separate semantic design from presentation.** In Rubric Generator v1.0, the Author produces a structured Rubric Spec after the authoring record. Program validation enforces identifiers, weights, score groups and portable input references; a deterministic renderer produces the final Markdown. Agents must not vary the final section numbering or reporting interface.
15. **Keep the final rubric portable and self-contained.** It may reference only files that actually ship inside the task directory, plus explicitly permitted live sources. Run manifests, authoring records, review files and other build artifacts may support authoring but must never become dependencies of the final `rubric.md`.

## Required working artifact

Create an authoring record before the rubric. It must contain:

- task type and evidence boundary;
- requirement-to-source traceability;
- source inventory reviewed;
- solve/verification passes and their checkable outputs;
- anchor ledger with provenance and tolerances;
- a two-layer judgment map: objective-verifiable checks plus allowed-variation boundaries;
- answer-space tests showing that contrasting but defensible paths can satisfy the rubric;
- known traps and error ownership;
- proposed scoring design, including each atom's layer, stable ID, primary ownership, weight, state function, formula, source requirement/anchor IDs, and justification for every non-prompt threshold;
- unresolved input gaps.

The authoring record is an internal build artifact, not part of the benchmark dataset and not a participant-facing answer.

The scoring angles and atom design must emerge from the author's solve/verification work—requirements, anchors, pitfalls, and observed failure modes—not from a preselected generic rubric filled in after the fact.

For this project, every Rubric Spec exposes one completion group and five quality groups: content coverage, accuracy/fidelity, format compliance, structure integrity, and hallucination/self-consistency. Each group has its own task-derived atoms whose weights sum to 100. The reporting shell is fixed; it does not authorize filler criteria. Completion is rendered as `0.00%—100.00%`; each quality group is rendered as `0.00—5.00`. No combined 0—100 total is emitted.

## Workflow and references

Follow [authoring-workflow.md](references/authoring-workflow.md). Use:

- [rubric-schema.md](references/rubric-schema.md) for the adaptive output contract;
- [deterministic-measurement.md](references/deterministic-measurement.md) for state functions, evidence, and stability;
- [task-type-guides.md](references/task-type-guides.md) for task-specific anchor methods;
- [temporal-fairness.md](references/temporal-fairness.md) for live, frozen, and versioned facts.

Static audit is necessary but never sufficient. A passing audit does not prove that anchors are true, thresholds are authorized, or the rubric fits the task.
