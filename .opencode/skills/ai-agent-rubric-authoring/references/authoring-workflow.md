# Authoring workflow

## 1. Inventory and freeze the evidence boundary

Read the full problem and every supplied source artifact. Record file identity, version, sheet/page/section structure, time cutoff, allowed external research, requested output, and forbidden actions. Do not read participant deliveries or reference scores.

## 2. Build the task contract

Create a traceability table with one row per explicit requirement:

`requirement_id | requirement | source | locator | mandatory/conditional | acceptable variation`

Separate explicit requirements from useful-but-optional qualities. A hard criterion or threshold without a traceable basis must be removed or clearly treated as non-mandatory.

## 3. Solve to audit

Perform evidence-producing verification passes before designing scores:

- objective/frozen tasks: extract and recompute exact gold values; cross-check important anchors by a second method when practical;
- document transformation: derive the required source facts and delivery constraints, then simulate what a correct transformed artifact must preserve;
- code/version tasks: inspect the exact version, reproduce the causal chain statically or in an allowed sandbox, and compare a known fix only as corroboration;
- open research: triangulate critical claims, define source precedence and cutoff, and map reasonable alternative conclusions;
- workflow/agent tasks: distinguish actual process evidence from labels or unsupported execution claims.

Do not expose chain-of-thought. Save only methods, commands or formulas, locators, results, conflicts, and conclusions needed for audit.

Use the solve/verification results to discover the scoring angles. Do not choose a generic set of dimensions or atoms first and then retrofit the task into them.

## 4. Create the anchor ledger

For every anchor record:

`anchor_id | type | expected value/proposition | source | locator | verification method | verification result | tolerance | confidence/boundary`

Use `Knn` for anchor IDs so they cannot be confused with `Ann` accuracy/fidelity scoring atoms. The cited source must directly support the anchor; do not infer a product fact from a different product, document context, or scenario.

Objective anchors require reproducible calculations. Open-task anchors describe mandatory truths or boundaries, not a single preferred essay. If an anchor cannot be verified, do not silently promote it to gold.

For live sources, open the exact page before recording it. Preserve its complete HTTPS URL, publisher, title, date/version, direct support scope, and verification evidence. Do not infer URLs from naming patterns. If access is restricted or temporarily unavailable, record that status and provide a stable official index, archive, corroborating authority, or other fallback; such a page cannot be the sole basis of a key anchor.

## 5. Build the two-layer judgment map

For every task, identify the objectively verifiable layer: explicit deliverable requirements, fixed-source facts, independently checkable claims, source quality, time/version boundaries, and observable evidence relationships.

For subjective or mixed tasks, separately define the allowed-variation layer. Record:

- what may legitimately differ, such as viewpoint, structure, method, prioritization, or conclusion;
- the minimum evidence and reasoning relationship that makes a chosen path defensible;
- concrete invalid boundaries, such as contradiction with verified facts, unsupported conclusion-changing claims, or internal inconsistency;
- contrasting examples as illustrations, never as an exhaustive answer list.

Run an answer-space test with contrasting defensible paths. The rubric should award both when they satisfy the shared minimum. If it rewards only the author's preferred path, revise the judgment boundary before drafting atoms.

Use `objective` when the core result is substantially fixed, `subjective` when the core response is open but still has objective baselines, and `mixed` when fixed-result and open-judgment subtasks are both substantial. An objective task leaves the allowed-variation layer and answer-space tests empty rather than inventing placeholders.

## 6. Design the task-native scoring model

Choose the smallest set of atoms that covers the actual construct. Give each requirement one primary scoring home. Use only applicable state functions. Preserve a fixed downstream reporting interface only when the evaluation system requires it; do not manufacture content requirements to fill a section.

For every numeric threshold not stated in the prompt, record its basis. Prefer natural denominators from the source or task. Remove arbitrary counts such as “at least 6 companies” or “12 facts” unless the task or defensible benchmark design explicitly establishes them.

Lock the planned atom contract in the authoring record before prose finalization: atom ID, evaluation layer, primary purpose, weight, state function, formula/state rule, and linked requirement or anchor IDs. Later wording may improve, but this contract may change only when source-backed analysis or review proves it wrong.

Before locking, confirm every mandatory requirement is directly tested by at least one scored state rule. Merely attaching a requirement ID is not sufficient. Define empty-set behavior for every `CLAIM-RATIO` and for any `RATIO` whose denominator can be zero, prefer natural denominators to invented thresholds, and reserve severe caps for material conclusion-changing failures.

## 7. Build and validate the Rubric Spec

Encode observable conditions, state formulas, evidence requirements, tolerances, N/A boundaries, and error ownership in the structured Rubric Spec. Include gold anchors when needed to judge objectively, but do not turn the rubric into a worked participant answer. Validate its schema, IDs, per-group weights, source paths and authoring-record alignment, then let the deterministic renderer create Markdown. Do not author an alternative free-form layout.

## 8. Independent review

The reviewer validates the existing authoring record and rubric. It must check:

- every hard requirement and threshold has a source;
- objective anchors are reproducible from the supplied gold source;
- subjective criteria distinguish objective checks from legitimate variation;
- contrasting evidence-supported answers can score well without matching the author's viewpoint or structure;
- each atom is computable and evidence is locatable;
- defects have unique ownership inside a metric;
- requested revisions were actually applied;
- the rubric has not grown new scope.

The reviewer may propose a concrete repair to a defect but must not replace the authoring design with its preferred rubric.

When a reviewer otherwise passes the rubric but leaves suggestions, independently classify those suggestions. Promote only a suggestion backed by a concrete, reproducible scoring counterexample. Do not run this extra classification when the reviewer already found blocking defects, and do not promote hypothetical overlap, wording preferences, or score-neutral usability improvements.

## 9. Bounded revision and arbitration

Revise only blocking defects, with at most one reviewer-directed revision in the default generator workflow. Preserve verified anchors and stable IDs unless the review demonstrates they are wrong. After that revision, an arbitrator resolves remaining disputes against the task contract and source evidence, not against this Skill by default.

## 10. Stability acceptance

Before freezing, compare semantic fingerprints across independent drafts or prior runs when available: explicit requirements; anchor IDs and values; hard thresholds and provenance; atom ownership and formulas; error/cap triggers. Any unexplained difference is a defect. Exact prose and table layout need not match.

## 11. Final audit

Run structural checks, then manually verify anchor fidelity, scope traceability, denominator clarity, non-overlap, format neutrality, and time/version fairness. Static success never substitutes for source verification. Static heuristics should distinguish objective blockers from advisories: absence of a keyword, a nonstandard layout, or a stylistic difference is not by itself a reason to reject or rewrite an otherwise usable rubric. Audit does not authorize edits.
