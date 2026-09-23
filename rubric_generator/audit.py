from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rubric_spec import METRICS, audit_rendered_rubric


CORE_CONCEPTS = [
    ("task/evidence boundary", r"任务|目标|证据|来源"),
    ("scoring contract", r"计分|评分|状态"),
    ("evaluation record", r"评测记录|评价记录|证据记录"),
]
FORBIDDEN = ["Success Rate", "锁定评分标准", "产品A", "产品B"]
ATOM_ID_RE = re.compile(r"[A-Z][A-Z0-9_-]*\d+")
WEIGHT_RE = re.compile(r"-?\d+(?:\.\d+)?")
FUNCTION_RE = re.compile(r"\b(?:BIN|RATIO|COUNT|CLAIM-RATIO)\b|状态\s*=")
NAMED_FUNCTION_RE = re.compile(r"(?<![A-Z-])(?:CLAIM-RATIO|RATIO|COUNT|BIN)(?![A-Z-])")
VAGUE_RE = re.compile(r"酌情|较好|视情况|合理|充分|清晰|重要|代表性|适当|优质|全面|深入")
REVIEW_CATEGORIES = {
    "task_fidelity", "anchor", "variation_fairness", "scope", "determinism", "evidence",
    "revision_application",
}
LAYER_ALIASES = {
    "objective-verifiable": "objective_verifiable",
    "objective_verifiable": "objective_verifiable",
    "可客观核验": "objective_verifiable",
    "客观核验": "objective_verifiable",
    "allowed-variation": "allowed_variation",
    "allowed_variation": "allowed_variation",
    "允许差异": "allowed_variation",
}

AtomRow = tuple[str, str, str, str | None]


@dataclass(frozen=True)
class AuditResult:
    path: Path
    issues: tuple[str, ...]
    advisories: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.issues


def audit_text_detailed(text: str) -> tuple[list[str], list[str]]:
    """Return objective blockers and non-blocking static observations separately."""
    issues: list[str] = []
    advisories: list[str] = []
    if not text.strip():
        return ["rubric is empty"], advisories
    for label, pattern in CORE_CONCEPTS:
        if not re.search(pattern, text):
            advisories.append(f"core concept not detected automatically: {label}")
    for phrase in FORBIDDEN:
        if phrase in text:
            advisories.append(f"possible legacy placeholder; confirm task relevance: {phrase}")

    rows = parse_atom_rows(text)
    if not rows:
        issues.append("no scored atoms matched the required scoring-table contract")
        return issues, advisories
    ids = [row[0] for row in rows]
    if len(ids) != len(set(ids)):
        issues.append("duplicate scored atom ID")
    for atom, weight, condition, _ in rows:
        if float(weight) <= 0:
            issues.append(f"{atom} has non-positive weight")
        if not FUNCTION_RE.search(condition):
            advisories.append(f"{atom}: named deterministic state function was not detected; confirm the prose rule is computable")
        vague = sorted(set(VAGUE_RE.findall(condition)))
        if vague and not re.search(r"定义|即|是指|满足.*条件|具体为", condition):
            advisories.append(f"{atom}: confirm these terms are operationalized: {','.join(vague)}")

    for prefix, total in _group_weight_totals(rows).items():
        if abs(total - 100.0) > 1e-9:
            advisories.append(f"{prefix} weights sum to {total}; confirm this matches the rubric's own normalization")
    if not re.search(r"文件|页|表|单元格|幻灯片|段|章节|代码|URL|locator|定位", text, re.I):
        advisories.append("evidence-locator convention was not detected automatically")
    return issues, advisories


def audit_text(text: str) -> list[str]:
    """Backward-compatible blocker-only view used by generation safeguards."""
    issues, _ = audit_text_detailed(text)
    return issues


def normalize_authoring_record(record: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Normalize only unambiguous enum spelling variants before strict audit.

    Raw model output remains in the event log. The returned record is a deep
    copy, and every applied normalization is returned for replayable tracing.
    Unknown values are deliberately left untouched so the audit still blocks
    unsupported provenance.
    """
    normalized = copy.deepcopy(record)
    changes: list[dict[str, str]] = []
    allowed = {"prompt", "source_natural_denominator", "benchmark_convention"}
    thresholds = normalized.get("thresholds")
    if isinstance(thresholds, list):
        for index, threshold in enumerate(thresholds):
            if not isinstance(threshold, dict):
                continue
            value = threshold.get("provenance")
            if not isinstance(value, str):
                continue
            candidate = re.sub(r"[\s-]+", "_", value.strip().casefold())
            if candidate in allowed and candidate != value:
                threshold["provenance"] = candidate
                changes.append({
                    "path": f"thresholds[{index}].provenance",
                    "from": value,
                    "to": candidate,
                })

    design = normalized.get("scoring_design")
    if isinstance(design, dict):
        groups: list[tuple[str, Any]] = [("completion_atoms", design.get("completion_atoms"))]
        quality = design.get("quality_metrics")
        if isinstance(quality, dict):
            groups.extend((f"quality_metrics.{key}", value) for key, value in quality.items())
        for group_name, atoms in groups:
            if not isinstance(atoms, list):
                continue
            for index, atom in enumerate(atoms):
                if not isinstance(atom, dict):
                    continue
                refs = atom.get("requirement_or_anchor_ids")
                if not isinstance(refs, list):
                    continue
                filtered = [value for value in refs if not re.fullmatch(r"[OV]\d{2,}", str(value))]
                if filtered != refs:
                    atom["requirement_or_anchor_ids"] = filtered
                    changes.append({
                        "path": f"scoring_design.{group_name}[{index}].requirement_or_anchor_ids",
                        "from": json.dumps(refs, ensure_ascii=False),
                        "to": json.dumps(filtered, ensure_ascii=False),
                    })
    return normalized, changes


def audit_authoring_record(record: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    reference_ids: set[str] = set()
    mandatory_requirement_ids: set[str] = set()
    required = [
        "task_type", "judgment_profile", "evidence_boundary", "requirements", "materials_reviewed",
        "verification_passes", "anchors", "evaluation_layers", "answer_space_tests", "pitfalls", "thresholds",
        "rejected_constraints", "scoring_design", "unresolved_input_gaps",
    ]
    for key in required:
        if key not in record:
            issues.append(f"authoring record missing key: {key}")
    profile = record.get("judgment_profile")
    if profile not in {"objective", "subjective", "mixed"}:
        issues.append("judgment_profile must be objective, subjective, or mixed")
    requirements = record.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        issues.append("authoring record has no traced requirements")
    else:
        for index, requirement in enumerate(requirements, 1):
            if not isinstance(requirement, dict):
                issues.append(f"requirement {index} is not an object")
                continue
            for key in ("id", "text", "source", "locator", "mandatory", "allowed_variation"):
                if key not in requirement:
                    issues.append(f"requirement {index} missing {key}")
            requirement_id = str(requirement.get("id", ""))
            if not re.fullmatch(r"REQ\d{2,}", requirement_id):
                issues.append(f"requirement {index} ID must use REQnn prefix")
            if requirement_id in reference_ids:
                issues.append(f"duplicate requirement or anchor ID: {requirement_id}")
            reference_ids.add(requirement_id)
            if requirement.get("mandatory") is True:
                mandatory_requirement_ids.add(requirement_id)
    passes = record.get("verification_passes")
    if not isinstance(passes, list) or len(passes) < 2:
        issues.append("authoring record requires at least two verifiable passes")
    anchors = record.get("anchors")
    if not isinstance(anchors, list):
        issues.append("anchors must be a list")
    else:
        for index, anchor in enumerate(anchors, 1):
            if not isinstance(anchor, dict):
                issues.append(f"anchor {index} is not an object")
                continue
            for key in ("id", "type", "value_or_proposition", "source", "locator", "verification_method", "verification_result", "tolerance", "confidence_boundary"):
                if key not in anchor:
                    issues.append(f"anchor {index} missing {key}")
            anchor_id = str(anchor.get("id", ""))
            if not re.fullmatch(r"K\d{2,}", anchor_id):
                issues.append(f"anchor {index} ID must use Knn prefix")
            if anchor_id in reference_ids:
                issues.append(f"duplicate requirement or anchor ID: {anchor_id}")
            reference_ids.add(anchor_id)
    layers = record.get("evaluation_layers")
    objective_layer: list[Any] = []
    variation_layer: list[Any] = []
    if not isinstance(layers, dict):
        issues.append("evaluation_layers must be an object")
    else:
        objective_value = layers.get("objective_verifiable")
        variation_value = layers.get("allowed_variation")
        if not isinstance(objective_value, list):
            issues.append("evaluation_layers.objective_verifiable must be a list")
        else:
            objective_layer = objective_value
            for index, item in enumerate(objective_layer, 1):
                if not isinstance(item, dict):
                    issues.append(f"objective layer item {index} is not an object")
                    continue
                for key in (
                    "id", "judgment", "requirement_or_anchor_ids", "verification_rule",
                    "expected_value_or_boundary", "tolerance",
                ):
                    if key not in item:
                        issues.append(f"objective layer item {index} missing {key}")
        if not isinstance(variation_value, list):
            issues.append("evaluation_layers.allowed_variation must be a list")
        else:
            variation_layer = variation_value
            for index, item in enumerate(variation_layer, 1):
                if not isinstance(item, dict):
                    issues.append(f"allowed-variation item {index} is not an object")
                    continue
                for key in (
                    "id", "variation_dimension", "valid_if", "minimum_evidence",
                    "invalid_if", "contrasting_valid_examples",
                ):
                    if key not in item:
                        issues.append(f"allowed-variation item {index} missing {key}")
    if profile in {"objective", "subjective", "mixed"} and not objective_layer:
        issues.append(f"{profile} judgment profile requires objective-verifiable entries")
    if profile in {"subjective", "mixed"} and not variation_layer:
        issues.append(f"{profile} judgment profile requires allowed-variation entries")

    answer_space_tests = record.get("answer_space_tests")
    if not isinstance(answer_space_tests, list):
        issues.append("answer_space_tests must be a list")
    elif profile in {"subjective", "mixed"}:
        if not answer_space_tests:
            issues.append(f"{profile} judgment profile requires an answer-space test")
        for index, item in enumerate(answer_space_tests, 1):
            if not isinstance(item, dict):
                issues.append(f"answer-space test {index} is not an object")
                continue
            for key in (
                "id", "contrasting_valid_paths", "shared_minimum",
                "rubric_expected_result", "exclusion_risk",
            ):
                if key not in item:
                    issues.append(f"answer-space test {index} missing {key}")
            paths = item.get("contrasting_valid_paths")
            if not isinstance(paths, list) or len(paths) < 2:
                issues.append(f"answer-space test {index} needs at least two contrasting valid paths")
    thresholds = record.get("thresholds")
    if isinstance(thresholds, list):
        allowed = {"prompt", "source_natural_denominator", "benchmark_convention"}
        for index, threshold in enumerate(thresholds, 1):
            if not isinstance(threshold, dict) or threshold.get("provenance") not in allowed:
                issues.append(f"threshold {index} lacks allowed provenance")
            elif not threshold.get("basis_locator"):
                issues.append(f"threshold {index} lacks basis locator")
    design = record.get("scoring_design")
    if not isinstance(design, dict):
        issues.append("scoring_design must be an object")
    else:
        completion_atoms = design.get("completion_atoms")
        quality_metrics = design.get("quality_metrics")
        groups: list[tuple[str, Any]] = [("completion", completion_atoms)]
        if not isinstance(quality_metrics, dict):
            issues.append("scoring_design.quality_metrics must be an object")
        else:
            expected_metrics = {key for key, _, _ in METRICS}
            missing = sorted(expected_metrics - set(quality_metrics))
            if missing:
                issues.append("scoring_design.quality_metrics missing: " + ", ".join(missing))
            groups.extend((key, quality_metrics.get(key)) for key, _, _ in METRICS)
        atom_ids: list[str] = []
        scored_reference_ids: set[str] = set()
        expected_prefixes = {"completion": "T", **{key: prefix for key, _, prefix in METRICS}}
        for group_name, atoms in groups:
            if not isinstance(atoms, list) or not atoms:
                issues.append(f"scoring_design {group_name} must contain planned atoms")
                continue
            total = 0.0
            for index, atom in enumerate(atoms, 1):
                if not isinstance(atom, dict):
                    issues.append(f"planned {group_name} atom {index} is not an object")
                    continue
                for key in (
                    "id", "evaluation_layer", "primary_purpose", "weight", "state_function",
                    "formula_or_state_rule", "empty_set_value", "empty_set_reason",
                    "requirement_or_anchor_ids",
                ):
                    if key not in atom:
                        issues.append(f"planned {group_name} atom {index} missing {key}")
                atom_id = str(atom.get("id", ""))
                atom_ids.append(atom_id)
                prefix = expected_prefixes[group_name]
                if not re.fullmatch(rf"{prefix}\d{{2,}}", atom_id):
                    issues.append(f"planned {group_name} atom {atom_id or index} must use {prefix}nn prefix")
                atom_layer = atom.get("evaluation_layer")
                if atom_layer not in {"objective_verifiable", "allowed_variation"}:
                    issues.append(f"planned atom {atom_id or index} has unsupported evaluation layer")
                elif profile == "objective" and atom_layer != "objective_verifiable":
                    issues.append(f"planned atom {atom_id or index} conflicts with objective judgment profile")
                if atom.get("state_function") not in {"BIN", "RATIO", "COUNT", "CLAIM-RATIO"}:
                    issues.append(f"planned atom {atom_id or index} has unsupported state function")
                try:
                    weight = float(atom.get("weight", 0))
                    if weight <= 0:
                        issues.append(f"planned atom {atom_id or index} has non-positive weight")
                    total += weight
                except (TypeError, ValueError):
                    issues.append(f"planned atom {atom_id or index} has invalid weight")
                references = atom.get("requirement_or_anchor_ids")
                if not isinstance(references, list):
                    issues.append(f"planned atom {atom_id or index} requirement_or_anchor_ids must be a list")
                elif not references:
                    issues.append(f"planned atom {atom_id or index} must trace to a requirement or anchor")
                else:
                    normalized_references = {str(value) for value in references}
                    scored_reference_ids.update(normalized_references)
                    unknown = sorted(normalized_references - reference_ids)
                    if unknown:
                        issues.append(
                            f"planned atom {atom_id or index} references unknown requirements or anchors: "
                            + ", ".join(unknown)
                        )
                state_function = atom.get("state_function")
                if state_function == "CLAIM-RATIO":
                    empty_value = atom.get("empty_set_value")
                    valid_empty_value = (
                        isinstance(empty_value, int)
                        and not isinstance(empty_value, bool)
                        and empty_value in (0, 1)
                    )
                    if not valid_empty_value:
                        issues.append(
                            f"planned atom {atom_id or index} CLAIM-RATIO empty_set_value must be 0 or 1"
                        )
                    reason = atom.get("empty_set_reason")
                    if not isinstance(reason, str) or not reason.strip():
                        issues.append(f"planned atom {atom_id or index} CLAIM-RATIO requires empty_set_reason")
                elif state_function == "RATIO":
                    empty_value = atom.get("empty_set_value")
                    reason = atom.get("empty_set_reason")
                    has_value = empty_value is not None
                    has_reason = isinstance(reason, str) and bool(reason.strip())
                    if has_value or has_reason:
                        valid_empty_value = (
                            isinstance(empty_value, int)
                            and not isinstance(empty_value, bool)
                            and empty_value in (0, 1)
                        )
                        if not valid_empty_value or not has_reason:
                            issues.append(
                                f"planned atom {atom_id or index} RATIO empty-set policy requires "
                                "empty_set_value 0 or 1 and non-empty empty_set_reason"
                            )
                elif atom.get("empty_set_value") is not None or atom.get("empty_set_reason") not in {"", None}:
                    issues.append(
                        f"planned atom {atom_id or index} {state_function} atom must not define an empty-set policy"
                    )
            if abs(total - 100.0) > 1e-6:
                issues.append(f"planned {group_name} atom weights must sum to 100, got {total:g}")
        if len(atom_ids) != len(set(atom_ids)):
            issues.append("planned atom IDs are not unique")
        unscored = sorted(mandatory_requirement_ids - scored_reference_ids)
        if unscored:
            issues.append("mandatory requirements have no scored atom: " + ", ".join(unscored))
    return issues


def audit_review_payload(review: dict[str, Any]) -> list[str]:
    """Validate reviewer output without treating its opinion as ground truth."""
    issues: list[str] = []
    blocking = review.get("blocking")
    verdict = review.get("verdict")
    if verdict not in {"passed", "revision_required"}:
        issues.append("review verdict must be passed or revision_required")
    if not isinstance(blocking, list):
        issues.append("review blocking must be a list")
        blocking = []
    if (verdict == "passed") != (len(blocking) == 0):
        issues.append("review verdict and blocking list are inconsistent")
    for index, item in enumerate(blocking, 1):
        if not isinstance(item, dict):
            issues.append(f"blocking {index} is not an object")
            continue
        for key in ("id", "category", "location", "evidence", "reason", "required_change"):
            if not item.get(key):
                issues.append(f"blocking {index} missing {key}")
        if item.get("category") not in REVIEW_CATEGORIES:
            issues.append(f"blocking {index} has unsupported category")
    if not isinstance(review.get("verified"), list):
        issues.append("review verified must be a list")
    if not isinstance(review.get("suggestions"), list):
        issues.append("review suggestions must be a list")
    return issues


def audit_review_gate_payload(gate: dict[str, Any], suggestion_count: int) -> list[str]:
    """Validate the independent severity gate applied to reviewer suggestions."""
    issues: list[str] = []
    verdict = gate.get("verdict")
    if verdict not in {"classification_passed", "promotion_required"}:
        issues.append("review gate verdict must be classification_passed or promotion_required")
    if not isinstance(gate.get("summary"), str) or not gate.get("summary", "").strip():
        issues.append("review gate summary is missing")
    promoted = gate.get("promoted")
    if not isinstance(promoted, list):
        issues.append("review gate promoted must be a list")
        promoted = []
    if (verdict == "classification_passed") != (len(promoted) == 0):
        issues.append("review gate verdict and promoted list are inconsistent")
    seen_indexes: set[int] = set()
    for index, item in enumerate(promoted, 1):
        if not isinstance(item, dict):
            issues.append(f"review gate promoted item {index} is not an object")
            continue
        for key in (
            "suggestion_index", "id", "category", "location", "evidence", "reason",
            "required_change",
        ):
            if item.get(key) is None or item.get(key) == "":
                issues.append(f"review gate promoted item {index} missing {key}")
        suggestion_index = item.get("suggestion_index")
        if not isinstance(suggestion_index, int) or not 1 <= suggestion_index <= suggestion_count:
            issues.append(f"review gate promoted item {index} has invalid suggestion_index")
        elif suggestion_index in seen_indexes:
            issues.append(f"review gate suggestion {suggestion_index} promoted more than once")
        else:
            seen_indexes.add(suggestion_index)
        if item.get("category") not in REVIEW_CATEGORIES:
            issues.append(f"review gate promoted item {index} has unsupported category")
    return issues


def apply_review_gate(review: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    """Promote score-impacting suggestions to blocking findings deterministically."""
    merged = copy.deepcopy(review)
    suggestions = merged.get("suggestions")
    suggestion_items = suggestions if isinstance(suggestions, list) else []
    promoted_items = gate.get("promoted")
    promoted = promoted_items if isinstance(promoted_items, list) else []
    promoted_indexes = {
        int(item["suggestion_index"])
        for item in promoted
        if isinstance(item, dict) and isinstance(item.get("suggestion_index"), int)
    }
    blocking = merged.get("blocking")
    blocking_items = blocking if isinstance(blocking, list) else []
    for item in promoted:
        if not isinstance(item, dict):
            continue
        blocking_items.append({
            key: item[key]
            for key in ("id", "category", "location", "evidence", "reason", "required_change")
        })
    merged["blocking"] = blocking_items
    merged["suggestions"] = [
        value for index, value in enumerate(suggestion_items, 1)
        if index not in promoted_indexes
    ]
    if promoted:
        merged["verdict"] = "revision_required"
        merged["summary"] = (
            str(merged.get("summary", "")).rstrip()
            + f" 分类门禁将 {len(promoted)} 条影响评分的建议提升为 blocking。"
        ).strip()
    return merged


def audit_arbitration_payload(arbitration: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    decisions = arbitration.get("decisions")
    if not isinstance(arbitration.get("summary"), str) or not arbitration.get("summary"):
        issues.append("arbitration summary is missing")
    if not isinstance(decisions, list) or not decisions:
        issues.append("arbitration decisions must be a non-empty list")
        decisions = []
    for index, item in enumerate(decisions, 1):
        if not isinstance(item, dict):
            issues.append(f"arbitration decision {index} is not an object")
            continue
        for key in ("issue_id", "decision", "basis", "binding_change"):
            if not item.get(key):
                issues.append(f"arbitration decision {index} missing {key}")
        if item.get("decision") not in {"accept", "reject", "modify"}:
            issues.append(f"arbitration decision {index} has unsupported decision")
    if not isinstance(arbitration.get("final_instructions"), list):
        issues.append("arbitration final_instructions must be a list")
    return issues


def audit_file(path: Path) -> AuditResult:
    rubric = path.resolve()
    if not rubric.is_file():
        return AuditResult(rubric, ("rubric file not found",))
    try:
        text = rubric.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return AuditResult(rubric, (f"rubric unreadable: {exc}",))
    if "<!-- rubric-schema-version: 1.0 -->" in text:
        issues, advisories = audit_rendered_rubric(text)
    else:
        issues, advisories = audit_text_detailed(text)
    return AuditResult(rubric, tuple(issues), tuple(advisories))


def audit_record_rubric_alignment(record: dict[str, Any], rubric_text: str) -> list[str]:
    """Check that the final prose implements the locked structured scoring design."""
    issues: list[str] = []
    planned_atoms = record.get("scoring_design", {}).get("atoms", [])
    planned = {
        str(item.get("id")): item
        for item in planned_atoms
        if isinstance(item, dict) and item.get("id")
    }
    actual_rows = parse_atom_rows(rubric_text)
    actual = {
        atom_id: (float(weight), condition, layer)
        for atom_id, weight, condition, layer in actual_rows
    }
    for atom_id, item in planned.items():
        if atom_id not in actual:
            issues.append(f"planned atom missing from rubric: {atom_id}")
            continue
        actual_weight, condition, actual_layer = actual[atom_id]
        try:
            planned_weight = float(item.get("weight"))
        except (TypeError, ValueError):
            continue
        if abs(planned_weight - actual_weight) > 1e-9:
            issues.append(f"atom weight differs from authoring record: {atom_id}")
        expected_function = str(item.get("state_function", ""))
        actual_functions = set(NAMED_FUNCTION_RE.findall(condition))
        if expected_function and expected_function not in actual_functions:
            issues.append(f"atom state function differs from authoring record: {atom_id}")
        expected_layer = str(item.get("evaluation_layer", ""))
        if actual_layer and expected_layer and actual_layer != expected_layer:
            issues.append(f"atom evaluation layer differs from authoring record: {atom_id}")
    for atom_id in actual:
        if atom_id not in planned:
            issues.append(f"rubric atom absent from authoring record: {atom_id}")
    return issues


def audit_dataset(root: Path) -> list[AuditResult]:
    from .dataset import discover_tasks

    return [audit_file(task.rubric_file) for task in discover_tasks(root)]


def semantic_fingerprint(record: dict[str, Any], rubric_text: str) -> dict[str, Any]:
    requirements = sorted(
        (str(item.get("id", "")), str(item.get("text", "")), bool(item.get("mandatory")))
        for item in record.get("requirements", []) if isinstance(item, dict)
    )
    anchors = sorted(
        (str(item.get("id", "")), str(item.get("value_or_proposition", "")), str(item.get("tolerance", "")))
        for item in record.get("anchors", []) if isinstance(item, dict)
    )
    thresholds = sorted(
        (str(item.get("value", "")), str(item.get("purpose", "")), str(item.get("provenance", "")), str(item.get("basis_locator", "")))
        for item in record.get("thresholds", []) if isinstance(item, dict)
    )
    evaluation_layers = record.get("evaluation_layers", {})
    objective_layer = sorted(
        (
            str(item.get("id", "")),
            _normalize(str(item.get("judgment", ""))),
            tuple(sorted(str(value) for value in item.get("requirement_or_anchor_ids", []))),
            _normalize(str(item.get("verification_rule", ""))),
            _normalize(str(item.get("expected_value_or_boundary", ""))),
            str(item.get("tolerance", "")),
        )
        for item in evaluation_layers.get("objective_verifiable", []) if isinstance(item, dict)
    )
    variation_layer = sorted(
        (
            str(item.get("id", "")),
            _normalize(str(item.get("variation_dimension", ""))),
            _normalize(str(item.get("valid_if", ""))),
            _normalize(str(item.get("minimum_evidence", ""))),
            _normalize(str(item.get("invalid_if", ""))),
        )
        for item in evaluation_layers.get("allowed_variation", []) if isinstance(item, dict)
    )
    planned_atoms = record.get("scoring_design", {}).get("atoms", [])
    atoms = sorted(
        (
            str(item.get("id", "")),
            str(item.get("evaluation_layer", "")),
            str(item.get("primary_purpose", "")),
            float(item.get("weight", 0)),
            str(item.get("state_function", "")),
            _normalize(str(item.get("formula_or_state_rule", ""))),
            tuple(sorted(str(value) for value in item.get("requirement_or_anchor_ids", []))),
        )
        for item in planned_atoms if isinstance(item, dict)
    )
    error_triggers = sorted(
        _normalize(str(value))
        for value in record.get("scoring_design", {}).get("error_or_cap_triggers", [])
    )
    actual_atom_contract = sorted(
        (atom, layer or "legacy_unspecified", float(weight), tuple(sorted(set(NAMED_FUNCTION_RE.findall(condition)))))
        for atom, weight, condition, layer in parse_atom_rows(rubric_text)
    )
    payload = {
        "judgment_profile": str(record.get("judgment_profile", "")),
        "requirements": requirements,
        "anchors": anchors,
        "objective_verifiable": objective_layer,
        "allowed_variation": variation_layer,
        "thresholds": thresholds,
        "atoms": atoms,
        "error_or_cap_triggers": error_triggers,
        "rubric_atom_contract": actual_atom_contract,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "components": payload}


def atom_statistics(text: str) -> dict[str, object]:
    rows = parse_atom_rows(text)
    by_prefix: dict[str, int] = {}
    for atom, _, _, _ in rows:
        prefix = _prefix(atom)
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
    return {
        "characters": len(text),
        "atom_count": len(rows),
        "atoms_by_group": by_prefix,
        "core_concepts_present": sum(1 for _, pattern in CORE_CONCEPTS if re.search(pattern, text)),
    }


def parse_atom_rows(text: str) -> list[AtomRow]:
    """Parse supported scoring tables while ignoring calculation examples.

    Legacy: ID | weight | condition | ...
    Layered: ID | layer | weight | condition | ...
    Extended: ID | layer | weight | criterion | state rule | evidence
    """
    rows: list[AtomRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        body = line[1:-1] if line.endswith("|") else line[1:]
        cells = [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)]
        if not cells or not ATOM_ID_RE.fullmatch(cells[0]):
            continue
        if len(cells) >= 3 and WEIGHT_RE.fullmatch(cells[1]):
            condition = cells[2]
            layer: str | None = None
            # A numeric third cell indicates an interpolation/example table,
            # not a scoring contract (for example T03 | 10 | 0.5 | 0.875).
            if WEIGHT_RE.fullmatch(condition):
                continue
            if len(cells) >= 5 and FUNCTION_RE.search(cells[3]):
                condition = f"{condition} | {cells[3]}"
                if len(cells) >= 6:
                    layer = LAYER_ALIASES.get(cells[4].casefold())
            elif len(cells) >= 4:
                layer = LAYER_ALIASES.get(cells[3].casefold())
            rows.append((cells[0], cells[1], condition, layer))
            continue
        layer = LAYER_ALIASES.get(cells[1].casefold()) if len(cells) >= 2 else None
        if len(cells) >= 4 and WEIGHT_RE.fullmatch(cells[2]):
            # Supports both the layered contract and older tables with a
            # descriptive checkpoint column before the numeric weight. Some
            # valid generated rubrics put the state rule in its own column;
            # combine it with the observable criterion for semantic checks.
            condition = cells[3]
            if WEIGHT_RE.fullmatch(condition):
                continue
            if layer is not None and len(cells) >= 6 and FUNCTION_RE.search(cells[4]):
                condition = f"{condition} | {cells[4]}"
            rows.append((cells[0], cells[2], condition, layer))
    return rows


def _group_weight_totals(rows: list[AtomRow]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for atom, weight, _, _ in rows:
        prefix = _prefix(atom)
        totals[prefix] = totals.get(prefix, 0.0) + float(weight)
    return totals


def _prefix(atom: str) -> str:
    match = re.match(r"[A-Z][A-Z0-9_-]*?(?=\d{2}$)", atom)
    return match.group(0) if match else atom


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
