# Structured rubric schema

The rubric must be self-contained. Rubric Generator v1.0 fixes the presentation shell and score interface while allowing the task to determine the atoms, anchors, error rules, and atom counts.

Self-contained also means portable: the final file may reference actual dataset inputs and public sources, but it must not mention manifests, authoring records, Rubric Specs, reviews, schema retries, run directories, or other construction artifacts.

Live sources are structured records rather than prose blobs. Each record has a stable source ID, publisher, page title, date/version, complete HTTPS URL, directly supported scope, verification status/evidence, and an optional fallback. Render them as independently clickable rows; keep factual values in the anchor ledger rather than embedding them into the source list.

## Core content

Every rubric needs:

1. task objective and evidence boundary;
2. verified anchors or judgment boundaries;
3. delivery interpretation when relevant;
4. scoring contract and evidence locators;
5. the smallest sufficient set of task-native criteria;
6. material errors/caps only when averages would mislead;
7. evaluation records needed to reproduce the result.

## Atom contract

Recommended columns are `ID | layer | weight | observable condition and state rule | required evidence`. Use `zero rule` or `applicability` columns only when they add precision. `layer` is `objective-verifiable` or `allowed-variation`. IDs must be stable, weights positive, and every atom must have exactly one primary purpose.

## Two-layer contract for subjective tasks

- **Objective-verifiable:** score explicit requirements, verifiable facts, source/cutoff compliance, and observable evidence relationships against recorded anchors.
- **Allowed-variation:** do not compare the delivery with one preferred answer. Score whether the chosen viewpoint, structure, method, prioritization, or conclusion satisfies the recorded minimum evidence, remains consistent with verified facts, and avoids the recorded invalid boundaries.

The rubric must state what may vary and what remains invariant. Examples of valid alternatives illustrate the boundary but never define a closed answer list.

## Reporting contract

The structured spec must contain these six independently calculated groups:

- task completion, rendered on `0.00%—100.00%`;
- content coverage, rendered on `0.00—5.00`;
- accuracy/fidelity, rendered on `0.00—5.00`;
- format compliance, rendered on `0.00—5.00`;
- structure integrity, rendered on `0.00—5.00`;
- hallucination/self-consistency, rendered on `0.00—5.00`.

Every group contains at least one applicable atom and its atom weights sum to 100. Their criteria must still come from the task:

- do not add filler atoms merely to populate a dimension;
- format measures explicit format obligations and observable usability, not taste;
- accuracy checks claims actually made against anchors;
- coverage checks task-required breadth, not an invented ideal outline;
- hallucination checks unsupported/fabricated claims and false execution assertions;
- structure checks whether required components and evidence relationships are navigable.

The harness may define scale normalization, but it does not authorize new task requirements.

The Author returns a JSON Rubric Spec. The Python renderer, not the model, writes the fixed Markdown sections. Dataset input references must be portable paths relative to the task directory and must resolve to real files. Internal build files such as manifests, authoring records, reviews and arbitration records are forbidden in the final rubric.

## Prohibited assumptions

Do not require a minimum atom count, all four state functions, a fixed number of sections/subconditions, uniform thresholds across tasks, a single canonical conclusion for an open problem, a preferred outline or reasoning style, or decorative file/chart/citation/section requirements not stated by the task.
