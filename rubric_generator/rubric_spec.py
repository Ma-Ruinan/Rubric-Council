from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "1.0"
STATE_FUNCTIONS = {"BIN", "RATIO", "COUNT", "CLAIM-RATIO"}
LAYERS = {"objective_verifiable", "allowed_variation"}
SOURCE_VERIFICATION_STATUSES = {"verified", "access_restricted", "temporarily_unavailable"}
SOURCE_STATUS_ALIASES = {
    "partial": "access_restricted",
    "partial_js_rendered": "access_restricted",
    "discovery_only": "access_restricted",
    "verified_but_superseded": "verified",
}
METRICS: tuple[tuple[str, str, str], ...] = (
    ("content_coverage", "内容覆盖度", "C"),
    ("accuracy_fidelity", "准确率·忠实度", "A"),
    ("format_compliance", "格式合规度", "F"),
    ("structure_integrity", "结构完整度", "S"),
    ("hallucination_consistency", "幻觉／自洽性", "H"),
)
METRIC_KEYS = {"completion", *(key for key, _, _ in METRICS)}
FORBIDDEN_PORTABLE_TOKENS = (
    ".rubric-generator",
    "material-manifest.json",
    "authoring-record",
    "authoring record",
    "authoring_record",
    "rubric-spec",
    "rubric spec",
    "schema-invalid",
    "schema retry",
    "review-01.json",
    "arbitration.json",
)
REQUIRED_HEADINGS = (
    "## 1. 任务目标与适用范围",
    "## 2. 输入、源素材与证据边界",
    "## 3. 交付物解释",
    "## 4. 题面要求溯源",
    "## 5. 已核验锚点与允许差异",
    "## 6. 任务完成率",
    "## 7. 五项质量评分",
    "### 7.1 内容覆盖度",
    "### 7.2 准确率·忠实度",
    "### 7.3 格式合规度",
    "### 7.4 结构完整度",
    "### 7.5 幻觉／自洽性",
    "## 8. 关键错误与分数上限",
    "## 9. 证据记录与计算规则",
    "## 10. 未解决输入缺口与判定边界",
)


def rubric_spec_schema_example() -> dict[str, Any]:
    atom = {
        "id": "T01",
        "layer": "objective_verifiable",
        "weight": 100,
        "primary_purpose": "",
        "state_function": "BIN",
        "state_rule": "",
        "empty_set_value": None,
        "empty_set_reason": "",
        "required_evidence": "",
        "requirement_or_anchor_ids": [],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "title": "",
        "task_objective": "",
        "evidence_boundary": {
            "dataset_inputs": [{"file": "问题描述.txt", "role": "题面"}],
            "live_sources": [{
                "id": "SRC01", "publisher": "", "title": "",
                "date_or_version": "", "url": "https://example.com/source",
                "supports": "", "verification_status": "verified",
                "verification_evidence": "", "fallback_url": "",
                "fallback_note": "",
            }],
            "cutoff_rule": "",
            "source_precedence": [],
        },
        "delivery_interpretation": [],
        "requirements": [{
            "id": "REQ01", "text": "", "locator": "问题描述.txt:1",
            "mandatory": True, "allowed_variation": "",
        }],
        "anchors": [{
            "id": "K01", "type": "mandatory_fact", "proposition": "",
            "source_locator": "", "verification": "", "tolerance_or_boundary": "",
        }],
        "allowed_variations": [{
            "id": "V01", "dimension": "", "valid_if": "", "invalid_if": "",
        }],
        "completion": {"atoms": [atom]},
        "quality_metrics": {
            key: {"atoms": [{**atom, "id": f"{prefix}01"}]}
            for key, _, prefix in METRICS
        },
        "error_rules": [{
            "id": "E01", "trigger": "",
            "effects": [{"metric": "accuracy_fidelity", "cap": 2.0, "atom_ids": []}],
        }],
        "evidence_record_requirements": [],
        "unresolved_input_gaps": [],
    }


def normalize_rubric_spec(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize unambiguous presentation variants without changing scoring semantics."""
    changes: list[dict[str, str]] = []
    boundary = spec.get("evidence_boundary")
    if isinstance(boundary, dict):
        sources = boundary.get("live_sources")
        if isinstance(sources, list):
            for index, source in enumerate(sources):
                if not isinstance(source, dict):
                    continue
                value = source.get("verification_status")
                if isinstance(value, str):
                    candidate = SOURCE_STATUS_ALIASES.get(value.strip().casefold())
                    if candidate:
                        source["verification_status"] = candidate
                        changes.append({
                            "path": f"evidence_boundary.live_sources[{index}].verification_status",
                            "from": value,
                            "to": candidate,
                        })

    interpretation = spec.get("delivery_interpretation")
    if isinstance(interpretation, list):
        normalized_items: list[Any] = []
        for index, item in enumerate(interpretation):
            if isinstance(item, dict):
                text = "；".join(
                    f"{key}：{value}" for key, value in item.items()
                    if value is not None and str(value).strip()
                )
                normalized_items.append(text)
                changes.append({
                    "path": f"delivery_interpretation[{index}]",
                    "from": "object",
                    "to": "string",
                })
            else:
                normalized_items.append(item)
        spec["delivery_interpretation"] = normalized_items
    return changes


def audit_rubric_spec(spec: dict[str, Any], task_dir: Path | None = None) -> list[str]:
    issues: list[str] = []
    if spec.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version must be {SCHEMA_VERSION}")
    for key in (
        "title", "task_objective", "evidence_boundary", "delivery_interpretation",
        "requirements", "anchors", "allowed_variations", "completion",
        "quality_metrics", "error_rules", "evidence_record_requirements",
        "unresolved_input_gaps",
    ):
        if key not in spec:
            issues.append(f"rubric spec missing key: {key}")
    for key in ("title", "task_objective"):
        if not isinstance(spec.get(key), str) or not str(spec.get(key)).strip():
            issues.append(f"rubric spec {key} must be a non-empty string")

    boundary = spec.get("evidence_boundary")
    if not isinstance(boundary, dict):
        issues.append("evidence_boundary must be an object")
    else:
        for key in ("dataset_inputs", "live_sources", "cutoff_rule", "source_precedence"):
            if key not in boundary:
                issues.append(f"evidence_boundary missing {key}")
        inputs = boundary.get("dataset_inputs")
        if not isinstance(inputs, list):
            issues.append("evidence_boundary.dataset_inputs must be a list")
        else:
            for index, item in enumerate(inputs, 1):
                if not isinstance(item, dict) or not item.get("file") or not item.get("role"):
                    issues.append(f"dataset input {index} requires file and role")
                    continue
                file_name = str(item["file"])
                path = PurePosixPath(file_name.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    issues.append(f"dataset input is not a portable relative path: {file_name}")
                if any(token.casefold() in file_name.casefold() for token in FORBIDDEN_PORTABLE_TOKENS):
                    issues.append(f"dataset input references internal build artifact: {file_name}")
                if task_dir is not None and not (task_dir / Path(*path.parts)).is_file():
                    issues.append(f"referenced dataset input does not exist: {file_name}")
        live_sources = boundary.get("live_sources")
        if not isinstance(live_sources, list):
            issues.append("evidence_boundary.live_sources must be a list")
        else:
            source_ids: set[str] = set()
            source_urls: set[str] = set()
            required_source_fields = (
                "id", "publisher", "title", "date_or_version", "url", "supports",
                "verification_status", "verification_evidence", "fallback_url", "fallback_note",
            )
            for index, source in enumerate(live_sources, 1):
                if not isinstance(source, dict):
                    issues.append(f"live source {index} is not an object")
                    continue
                for field in required_source_fields:
                    if field not in source:
                        issues.append(f"live source {index} missing {field}")
                source_id = str(source.get("id", ""))
                if not re.fullmatch(r"SRC\d{2,}", source_id):
                    issues.append(f"live source {index} ID must use SRCnn prefix")
                if source_id in source_ids:
                    issues.append(f"duplicate live source ID: {source_id}")
                source_ids.add(source_id)
                for field in ("publisher", "title", "date_or_version", "supports", "verification_evidence"):
                    if not isinstance(source.get(field), str) or not source.get(field, "").strip():
                        issues.append(f"live source {source_id or index} requires non-empty {field}")
                url = str(source.get("url", ""))
                if not _is_https_url(url):
                    issues.append(f"live source {source_id or index} requires an absolute HTTPS URL")
                normalized_url = url.casefold().rstrip("/")
                if normalized_url in source_urls:
                    issues.append(f"duplicate live source URL: {url}")
                source_urls.add(normalized_url)
                status = source.get("verification_status")
                if status not in SOURCE_VERIFICATION_STATUSES:
                    issues.append(f"live source {source_id or index} has unsupported verification_status")
                fallback_url = source.get("fallback_url")
                fallback_note = source.get("fallback_note")
                if not isinstance(fallback_url, str) or not isinstance(fallback_note, str):
                    issues.append(f"live source {source_id or index} fallback fields must be strings")
                else:
                    if fallback_url and not _is_https_url(fallback_url):
                        issues.append(f"live source {source_id or index} fallback_url must be an absolute HTTPS URL")
                    if status != "verified" and not fallback_note.strip():
                        issues.append(
                            f"live source {source_id or index} requires fallback_note when not verified"
                        )

    _audit_object_list(spec, "requirements", ("id", "text", "locator", "mandatory", "allowed_variation"), issues)
    reference_ids: set[str] = set()
    mandatory_requirement_ids: set[str] = set()
    for index, requirement in enumerate(spec.get("requirements", []) if isinstance(spec.get("requirements"), list) else [], 1):
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("id", ""))
        if not re.fullmatch(r"REQ\d{2,}", requirement_id):
            issues.append(f"requirement {index} ID must use REQnn prefix")
        if requirement_id in reference_ids:
            issues.append(f"duplicate requirement or anchor ID: {requirement_id}")
        reference_ids.add(requirement_id)
        for field in ("text", "locator"):
            if not isinstance(requirement.get(field), str) or not requirement.get(field, "").strip():
                issues.append(f"requirement {index} requires non-empty {field}")
        if not isinstance(requirement.get("mandatory"), bool):
            issues.append(f"requirement {index} mandatory must be boolean")
        elif requirement["mandatory"]:
            mandatory_requirement_ids.add(requirement_id)
        if not isinstance(requirement.get("allowed_variation"), str):
            issues.append(f"requirement {index} allowed_variation must be a string")
    _audit_object_list(
        spec, "anchors",
        ("id", "type", "proposition", "source_locator", "verification", "tolerance_or_boundary"),
        issues, allow_empty=True,
    )
    for index, anchor in enumerate(spec.get("anchors", []) if isinstance(spec.get("anchors"), list) else [], 1):
        if not isinstance(anchor, dict):
            continue
        anchor_id = str(anchor.get("id", ""))
        if not re.fullmatch(r"K\d{2,}", anchor_id):
            issues.append(f"anchor {index} ID must use Knn prefix")
        if anchor_id in reference_ids:
            issues.append(f"duplicate requirement or anchor ID: {anchor_id}")
        reference_ids.add(anchor_id)
        if anchor.get("type") not in {
            "mandatory_fact", "objective_value", "boundary", "allowed_alternative"
        }:
            issues.append(f"anchor {index} has unsupported type")
        for field in ("proposition", "source_locator", "verification"):
            if not isinstance(anchor.get(field), str) or not anchor.get(field, "").strip():
                issues.append(f"anchor {index} requires non-empty {field}")
    _audit_object_list(
        spec, "allowed_variations", ("id", "dimension", "valid_if", "invalid_if"),
        issues, allow_empty=True,
    )
    variation_ids: set[str] = set()
    for index, variation in enumerate(spec.get("allowed_variations", []) if isinstance(spec.get("allowed_variations"), list) else [], 1):
        if not isinstance(variation, dict):
            continue
        variation_id = str(variation.get("id", ""))
        if not re.fullmatch(r"V\d{2,}", variation_id):
            issues.append(f"allowed variation {index} ID must use Vnn prefix")
        if variation_id in variation_ids:
            issues.append(f"duplicate allowed variation ID: {variation_id}")
        variation_ids.add(variation_id)
        for field in ("dimension", "valid_if", "invalid_if"):
            if not isinstance(variation.get(field), str) or not variation.get(field, "").strip():
                issues.append(f"allowed variation {index} requires non-empty {field}")

    seen_ids: set[str] = set()
    scored_reference_ids: set[str] = set()
    completion = spec.get("completion")
    if not isinstance(completion, dict):
        issues.append("completion must be an object")
    else:
        _audit_atom_group(
            completion.get("atoms"), "completion", "T", seen_ids,
            reference_ids, scored_reference_ids, issues,
        )

    quality = spec.get("quality_metrics")
    if not isinstance(quality, dict):
        issues.append("quality_metrics must be an object")
    else:
        expected = {key for key, _, _ in METRICS}
        missing = sorted(expected - set(quality))
        extra = sorted(set(quality) - expected)
        if missing:
            issues.append("quality_metrics missing: " + ", ".join(missing))
        if extra:
            issues.append("quality_metrics has unsupported keys: " + ", ".join(extra))
        for key, _, prefix in METRICS:
            group = quality.get(key)
            if not isinstance(group, dict):
                continue
            _audit_atom_group(
                group.get("atoms"), key, prefix, seen_ids,
                reference_ids, scored_reference_ids, issues,
            )

    unscored_requirements = sorted(mandatory_requirement_ids - scored_reference_ids)
    if unscored_requirements:
        issues.append(
            "mandatory requirements have no scored atom: " + ", ".join(unscored_requirements)
        )

    error_rules = spec.get("error_rules")
    if not isinstance(error_rules, list):
        issues.append("error_rules must be a list")
    else:
        for index, rule in enumerate(error_rules, 1):
            if not isinstance(rule, dict) or not rule.get("id") or not rule.get("trigger"):
                issues.append(f"error rule {index} requires id and trigger")
                continue
            effects = rule.get("effects")
            if not isinstance(effects, list) or not effects:
                issues.append(f"error rule {index} requires effects")
                continue
            for effect_index, effect in enumerate(effects, 1):
                if not isinstance(effect, dict) or effect.get("metric") not in METRIC_KEYS:
                    issues.append(f"error rule {index} effect {effect_index} has invalid metric")
                    continue
                cap = effect.get("cap")
                if cap is not None:
                    try:
                        value = float(cap)
                        upper = 100.0 if effect.get("metric") == "completion" else 5.0
                        if value < 0 or value > upper:
                            issues.append(f"error rule {index} effect {effect_index} cap out of range")
                    except (TypeError, ValueError):
                        issues.append(f"error rule {index} effect {effect_index} cap is invalid")
                atom_ids = effect.get("atom_ids", [])
                if not isinstance(atom_ids, list):
                    issues.append(f"error rule {index} effect {effect_index} atom_ids must be a list")
                else:
                    unknown = [str(atom) for atom in atom_ids if str(atom) not in seen_ids]
                    if unknown:
                        issues.append(
                            f"error rule {index} effect {effect_index} references unknown atoms: "
                            + ", ".join(unknown)
                        )

    for key in ("delivery_interpretation", "evidence_record_requirements", "unresolved_input_gaps"):
        value = spec.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            issues.append(f"{key} must be a list of strings")

    portable_text = json.dumps(spec, ensure_ascii=False)
    for token in FORBIDDEN_PORTABLE_TOKENS:
        if token.casefold() in portable_text.casefold():
            issues.append(f"rubric spec leaks internal build reference: {token}")
    return _dedupe(issues)


def audit_authoring_record_spec_alignment(record: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    design = record.get("scoring_design")
    if not isinstance(design, dict):
        return ["authoring record has no scoring_design"]
    planned = _record_atom_map(design)
    actual = {str(atom.get("id")): atom for _, atom in iter_spec_atoms(spec)}
    for atom_id, planned_atom in planned.items():
        actual_atom = actual.get(atom_id)
        if actual_atom is None:
            issues.append(f"planned atom missing from rubric spec: {atom_id}")
            continue
        for record_key, spec_key in (
            ("output_metric", "_metric"),
            ("evaluation_layer", "layer"),
            ("weight", "weight"),
            ("state_function", "state_function"),
            ("empty_set_value", "empty_set_value"),
            ("requirement_or_anchor_ids", "requirement_or_anchor_ids"),
        ):
            expected = planned_atom.get(record_key)
            observed = actual_atom.get(spec_key)
            if record_key == "weight":
                try:
                    if abs(float(expected) - float(observed)) > 1e-9:
                        issues.append(f"atom {atom_id} differs on weight")
                except (TypeError, ValueError):
                    issues.append(f"atom {atom_id} has invalid aligned weight")
            elif record_key == "requirement_or_anchor_ids":
                expected_refs = sorted(str(value) for value in expected or [])
                observed_refs = sorted(str(value) for value in observed or [])
                if expected_refs != observed_refs:
                    issues.append(f"atom {atom_id} differs on {record_key}")
            elif record_key == "empty_set_value":
                if expected != observed:
                    issues.append(f"atom {atom_id} differs on {record_key}")
            elif _norm(expected) != _norm(observed):
                issues.append(f"atom {atom_id} differs on {record_key}")
    for atom_id in actual:
        if atom_id not in planned:
            issues.append(f"rubric spec atom absent from authoring record: {atom_id}")
    return issues


def render_rubric(spec: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# Rubric：{spec['title']}",
        "",
        "<!-- rubric-schema-version: 1.0 -->",
        "",
        "## 1. 任务目标与适用范围",
        "",
        str(spec["task_objective"]).strip(),
        "",
        "本 Rubric 分别输出任务完成率与五项质量分，不生成混合的 0—100 综合总分。",
        "",
        "## 2. 输入、源素材与证据边界",
        "",
    ]
    boundary = spec["evidence_boundary"]
    inputs = boundary.get("dataset_inputs", [])
    if inputs:
        lines.extend(["| 数据集内输入 | 作用 |", "| --- | --- |"])
        for item in inputs:
            lines.append(f"| `{_esc(item['file'])}` | {_esc(item['role'])} |")
    else:
        lines.append("本题无额外问题源素材。")
    live_sources = boundary.get("live_sources", [])
    lines.extend(["", "### 2.1 实时或外部来源", ""])
    if live_sources:
        lines.extend([
            "| ID | 发布方 | 来源名称 | 日期／版本 | 支持内容 | 验证状态 | 主链接 | 备用证据 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        status_labels = {
            "verified": "已访问并核验",
            "access_restricted": "访问受限",
            "temporarily_unavailable": "暂时不可访问",
        }
        for source in live_sources:
            main_link = f"<{source['url']}>"
            fallback_parts = []
            if source.get("fallback_url"):
                fallback_parts.append(f"<{source['fallback_url']}>")
            if source.get("fallback_note"):
                fallback_parts.append(str(source["fallback_note"]))
            fallback = "；".join(fallback_parts) or "—"
            status = status_labels.get(source.get("verification_status"), source.get("verification_status", ""))
            evidence = str(source.get("verification_evidence", "")).strip()
            status_text = f"{status}：{evidence}"
            lines.append(
                f"| {_esc(source['id'])} | {_esc(source['publisher'])} | {_esc(source['title'])} | "
                f"{_esc(source['date_or_version'])} | {_esc(source['supports'])} | {_esc(status_text)} | "
                f"{main_link} | {_esc(fallback)} |"
            )
    else:
        lines.append("本题不依赖实时或外部来源。")
    lines.extend(["", "### 2.2 时间、版本与来源优先级", ""])
    lines.append(f"- **时间／版本边界**：{_text(boundary.get('cutoff_rule'))}")
    lines.append(f"- **来源优先级**：{_join(boundary.get('source_precedence'))}")

    lines.extend(["", "## 3. 交付物解释", ""])
    lines.extend(_bullets(spec.get("delivery_interpretation"), "本题没有额外的交付解释。"))

    lines.extend(["", "## 4. 题面要求溯源", "", "| ID | 要求 | 题面定位 | 必做 | 允许差异 |", "| --- | --- | --- | --- | --- |"])
    for item in spec["requirements"]:
        lines.append(
            f"| {_esc(item['id'])} | {_esc(item['text'])} | {_esc(item['locator'])} | "
            f"{'是' if item['mandatory'] else '否'} | {_esc(item['allowed_variation'])} |"
        )

    lines.extend(["", "## 5. 已核验锚点与允许差异", "", "### 5.1 已核验锚点", ""])
    anchors = spec.get("anchors", [])
    if anchors:
        lines.extend(["| ID | 类型 | 命题／数值 | 来源定位 | 核验 | 容差／边界 |", "| --- | --- | --- | --- | --- | --- |"])
        for item in anchors:
            lines.append(
                f"| {_esc(item['id'])} | {_esc(item['type'])} | {_esc(item['proposition'])} | "
                f"{_esc(item['source_locator'])} | {_esc(item['verification'])} | "
                f"{_esc(item['tolerance_or_boundary'])} |"
            )
    else:
        lines.append("本题没有需要写入最终 Rubric 的封闭事实锚点；按题面和证据边界判定。")
    lines.extend(["", "### 5.2 允许差异", ""])
    variations = spec.get("allowed_variations", [])
    if variations:
        lines.extend(["| ID | 可变化维度 | 有效条件 | 失效边界 |", "| --- | --- | --- | --- |"])
        for item in variations:
            lines.append(
                f"| {_esc(item['id'])} | {_esc(item['dimension'])} | {_esc(item['valid_if'])} | "
                f"{_esc(item['invalid_if'])} |"
            )
    else:
        lines.append("本题核心结果为客观结果，不设置开放结论方向。")

    lines.extend(["", "## 6. 任务完成率", ""])
    lines.extend(_render_atom_table(spec["completion"]["atoms"]))
    lines.extend([
        "",
        "**任务完成率 = Σ（原子权重 × 原子状态），范围 0.00%—100.00%。**",
        "完成率只回答题目组成是否完成；事实正确性、内容质量和编造问题由五项质量指标处理。",
        "",
        "## 7. 五项质量评分",
        "",
        "每项质量分 = 5 × Σ（项内原子权重 × 原子状态）÷ 100，范围 0.00—5.00。",
    ])
    quality = spec["quality_metrics"]
    for index, (key, label, _) in enumerate(METRICS, 1):
        lines.extend(["", f"### 7.{index} {label}", ""])
        lines.extend(_render_atom_table(quality[key]["atoms"]))

    lines.extend(["", "## 8. 关键错误与分数上限", ""])
    rules = spec.get("error_rules", [])
    if rules:
        lines.extend(["| 错误代码 | 可观察触发条件 | 影响 |", "| --- | --- | --- |"])
        for rule in rules:
            effects = []
            for effect in rule["effects"]:
                metric = _metric_label(effect["metric"])
                cap = effect.get("cap")
                atoms = "、".join(str(value) for value in effect.get("atom_ids", [])) or "无指定原子"
                cap_text = "无额外上限" if cap is None else f"上限 {float(cap):g}"
                effects.append(f"{metric}：{cap_text}；原子 {atoms}")
            lines.append(f"| {_esc(rule['id'])} | {_esc(rule['trigger'])} | {_esc('；'.join(effects))} |")
    else:
        lines.append("本题不设置额外错误代码或分数上限；全部结果由原子状态计算。")

    lines.extend([
        "",
        "同一具体缺陷在同一指标内只由一个主责原子扣分；跨指标影响必须对应不同的可观察后果。多个上限同时触发时取最低上限。",
        "",
        "## 9. 证据记录与计算规则",
        "",
        "- 每个原子必须记录：状态、权重、得分贡献、交付文件名、可定位证据和判分原因。",
        "- `BIN` 取 0 或 1；`RATIO`、`COUNT`、`CLAIM-RATIO` 按原子状态规则折算至 0—1。",
        "- 找不到证据记为未满足；不得以整体印象补分。所有乘法、求和、平均和上限应用应由计算工具执行。",
    ])
    if boundary.get("live_sources"):
        lines.append(
            "- 对实时或历史来源，应按参测对象的执行／截止时点核验；评测时链接失效本身不等于当时虚构，"
            "应优先查归档、同时间接证据或同期权威来源，并记录无法复核的限制。"
        )
    lines.extend(_bullets(spec.get("evidence_record_requirements"), "无额外证据记录要求。"))
    lines.extend([
        "- 单题质量均分为五项质量分的算术平均值；任务完成率单独展示，不并入质量均分。",
        "",
        "## 10. 未解决输入缺口与判定边界",
        "",
    ])
    lines.extend(_bullets(spec.get("unresolved_input_gaps"), "没有影响当前判分合同执行的未解决输入缺口。"))
    return "\n".join(lines).rstrip() + "\n"


def audit_rendered_rubric(text: str) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    advisories: list[str] = []
    if "<!-- rubric-schema-version: 1.0 -->" not in text:
        issues.append("missing rubric schema version marker")
    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            issues.append(f"missing required rendered heading: {heading}")
    for token in FORBIDDEN_PORTABLE_TOKENS:
        if token.casefold() in text.casefold():
            issues.append(f"rendered rubric leaks internal build reference: {token}")
    if "0.00%—100.00%" not in text or "0.00—5.00" not in text:
        advisories.append("score-range wording differs from the standard reporting interface")
    return _dedupe(issues), _dedupe(advisories)


def semantic_fingerprint(record: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    atoms = sorted(
        (
            metric, str(atom.get("id")), str(atom.get("layer")), float(atom.get("weight", 0)),
            str(atom.get("state_function")), _norm(atom.get("state_rule")),
            atom.get("empty_set_value"), _norm(atom.get("empty_set_reason")),
            tuple(sorted(str(value) for value in atom.get("requirement_or_anchor_ids", []))),
        )
        for metric, atom in iter_spec_atoms(spec)
    )
    payload = {
        "schema_version": spec.get("schema_version"),
        "judgment_profile": record.get("judgment_profile"),
        "requirements": sorted(
            (str(item.get("id")), _norm(item.get("text")), bool(item.get("mandatory")))
            for item in record.get("requirements", []) if isinstance(item, dict)
        ),
        "anchors": sorted(
            (str(item.get("id")), _norm(item.get("value_or_proposition")), _norm(item.get("tolerance")))
            for item in record.get("anchors", []) if isinstance(item, dict)
        ),
        "atoms": atoms,
        "error_rules": spec.get("error_rules", []),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "components": payload}


def iter_spec_atoms(spec: dict[str, Any]):
    for atom in spec.get("completion", {}).get("atoms", []):
        if isinstance(atom, dict):
            yield "completion", {**atom, "_metric": "completion"}
    quality = spec.get("quality_metrics", {})
    for key, _, _ in METRICS:
        for atom in quality.get(key, {}).get("atoms", []):
            if isinstance(atom, dict):
                yield key, {**atom, "_metric": key}


def _audit_atom_group(
    atoms: Any,
    metric: str,
    prefix: str,
    seen_ids: set[str],
    reference_ids: set[str],
    scored_reference_ids: set[str],
    issues: list[str],
) -> None:
    if not isinstance(atoms, list) or not atoms:
        issues.append(f"{metric} must contain at least one scoring atom")
        return
    weight_total = 0.0
    for index, atom in enumerate(atoms, 1):
        if not isinstance(atom, dict):
            issues.append(f"{metric} atom {index} is not an object")
            continue
        for key in (
            "id", "layer", "weight", "primary_purpose", "state_function", "state_rule",
            "empty_set_value", "empty_set_reason", "required_evidence", "requirement_or_anchor_ids",
        ):
            if key not in atom:
                issues.append(f"{metric} atom {index} missing {key}")
        atom_id = str(atom.get("id", ""))
        if not re.fullmatch(rf"{re.escape(prefix)}\d{{2,}}", atom_id):
            issues.append(f"{metric} atom {index} ID must use {prefix}nn prefix")
        if atom_id in seen_ids:
            issues.append(f"duplicate atom ID: {atom_id}")
        seen_ids.add(atom_id)
        if atom.get("layer") not in LAYERS:
            issues.append(f"{atom_id or metric} has invalid layer")
        if atom.get("state_function") not in STATE_FUNCTIONS:
            issues.append(f"{atom_id or metric} has invalid state function")
        for key in ("primary_purpose", "state_rule", "required_evidence"):
            if not isinstance(atom.get(key), str) or not atom.get(key, "").strip():
                issues.append(f"{atom_id or metric} requires non-empty {key}")
        if not isinstance(atom.get("requirement_or_anchor_ids"), list):
            issues.append(f"{atom_id or metric} requirement_or_anchor_ids must be a list")
        elif not atom.get("requirement_or_anchor_ids"):
            issues.append(f"{atom_id or metric} must trace to at least one requirement or anchor")
        else:
            scored_reference_ids.update(str(reference) for reference in atom["requirement_or_anchor_ids"])
            unknown = [
                str(reference) for reference in atom["requirement_or_anchor_ids"]
                if str(reference) not in reference_ids
            ]
            if unknown:
                issues.append(
                    f"{atom_id or metric} references unknown requirements or anchors: "
                    + ", ".join(unknown)
                )
        if atom.get("state_function") == "CLAIM-RATIO":
            empty_value = atom.get("empty_set_value")
            valid_empty_value = (
                isinstance(empty_value, int)
                and not isinstance(empty_value, bool)
                and empty_value in (0, 1)
            )
            if not valid_empty_value:
                issues.append(f"{atom_id or metric} CLAIM-RATIO empty_set_value must be 0 or 1")
            if not isinstance(atom.get("empty_set_reason"), str) or not atom.get("empty_set_reason", "").strip():
                issues.append(f"{atom_id or metric} CLAIM-RATIO requires empty_set_reason")
        elif atom.get("empty_set_value") is not None or atom.get("empty_set_reason") not in {"", None}:
            issues.append(f"{atom_id or metric} non-CLAIM-RATIO atom must not define an empty-set policy")
        try:
            weight = float(atom.get("weight"))
            if weight <= 0:
                issues.append(f"{atom_id or metric} has non-positive weight")
            weight_total += weight
        except (TypeError, ValueError):
            issues.append(f"{atom_id or metric} has invalid weight")
    if abs(weight_total - 100.0) > 1e-6:
        issues.append(f"{metric} atom weights must sum to 100, got {weight_total:g}")


def _audit_object_list(
    source: dict[str, Any],
    key: str,
    required: tuple[str, ...],
    issues: list[str],
    allow_empty: bool = False,
) -> None:
    value = source.get(key)
    if not isinstance(value, list) or (not value and not allow_empty):
        issues.append(f"{key} must be {'a' if allow_empty else 'a non-empty'} list")
        return
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            issues.append(f"{key} item {index} is not an object")
            continue
        for field in required:
            if field not in item:
                issues.append(f"{key} item {index} missing {field}")


def _record_atom_map(design: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for atom in design.get("completion_atoms", []):
        if isinstance(atom, dict) and atom.get("id"):
            result[str(atom["id"])] = {**atom, "output_metric": "completion"}
    quality = design.get("quality_metrics", {})
    if isinstance(quality, dict):
        for key, _, _ in METRICS:
            for atom in quality.get(key, []):
                if isinstance(atom, dict) and atom.get("id"):
                    result[str(atom["id"])] = {**atom, "output_metric": key}
    return result


def _render_atom_table(atoms: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| ID | 判据层 | 权重 | 唯一目的 | 状态函数与可观察规则 | 必需证据 | 溯源 |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for atom in atoms:
        trace = "、".join(str(value) for value in atom.get("requirement_or_anchor_ids", [])) or "—"
        rule = f"**{atom['state_function']}**：{atom['state_rule']}"
        if atom["state_function"] == "CLAIM-RATIO":
            empty_value = atom.get("empty_set_value")
            rule += f"；空集合状态={empty_value}（{atom.get('empty_set_reason', '')}）"
        lines.append(
            f"| {_esc(atom['id'])} | {_esc(atom['layer'])} | {float(atom['weight']):g} | "
            f"{_esc(atom['primary_purpose'])} | {_esc(rule)} | {_esc(atom['required_evidence'])} | {_esc(trace)} |"
        )
    return lines


def _metric_label(metric: str) -> str:
    if metric == "completion":
        return "任务完成率"
    return next((label for key, label, _ in METRICS if key == metric), metric)


def _bullets(value: Any, empty: str) -> list[str]:
    if not isinstance(value, list) or not value:
        return [empty]
    return [f"- {_text(item)}" for item in value]


def _join(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "无"
    return "；".join(_text(item) for item in value)


def _is_https_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    placeholder = host in {"example.com", "example.org", "example.net", "localhost"} or host.endswith(".invalid")
    return (
        parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username
        and not parsed.password and not placeholder
    )


def _text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "无"


def _esc(value: Any) -> str:
    return _text(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
