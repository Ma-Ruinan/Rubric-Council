from __future__ import annotations

import json
from pathlib import Path

from .rubric_spec import rubric_spec_schema_example


REPORTING_CONTRACT = """固定下游接口（不得改名、合并或替换）：
1. 任务完成率：0.00%—100.00%；
2. 内容覆盖度：0.00—5.00；
3. 准确率·忠实度：0.00—5.00；
4. 格式合规度：0.00—5.00；
5. 结构完整度：0.00—5.00；
6. 幻觉／自洽性：0.00—5.00。
任务完成率回答“有没有完成题目组成”；五项质量回答“完成得怎么样”。不得再设计混合的 0—100 综合总分。每组原子权重必须各自合计 100。"""


def _path(path: Path, project_dir: Path) -> str:
    resolved = path.resolve()
    project = project_dir.resolve()
    try:
        return resolved.relative_to(project).as_posix()
    except ValueError as exc:
        raise ValueError(f"Agent input must stay inside the project: {resolved}") from exc


def _json_file(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_input_packet(task_dir: Path, material_manifest: Path) -> dict[str, object]:
    return {
        "problem": (task_dir / "问题描述.txt").read_text(encoding="utf-8-sig"),
        "material_manifest": _json_file(material_manifest),
    }


def _record_schema() -> str:
    atom = {
        "id": "T01", "evaluation_layer": "objective_verifiable",
        "primary_purpose": "", "weight": 100, "state_function": "BIN",
        "formula_or_state_rule": "", "empty_set_value": None,
        "empty_set_reason": "", "requirement_or_anchor_ids": [],
    }
    value = {
        "task_type": "objective_frozen | document_transform | versioned_code | open_research | workflow_evidence | mixed",
        "judgment_profile": "objective | subjective | mixed",
        "evidence_boundary": {"frozen": [], "live": [], "cutoff_or_version": "", "precedence": []},
        "requirements": [{"id": "REQ01", "text": "", "source": "problem", "locator": "", "mandatory": True, "allowed_variation": ""}],
        "materials_reviewed": [{"path": "", "sha256": "", "inspection": ""}],
        "verification_passes": [{"id": "VP01", "purpose": "", "method_or_command": "", "source_locators": [], "result": "", "cross_check": ""}],
        "anchors": [{"id": "K01", "type": "objective_value | mandatory_fact | boundary | allowed_alternative", "value_or_proposition": "", "source": "", "locator": "", "verification_method": "", "verification_result": "", "tolerance": "", "confidence_boundary": ""}],
        "evaluation_layers": {
            "objective_verifiable": [{"id": "O01", "judgment": "", "requirement_or_anchor_ids": [], "verification_rule": "", "expected_value_or_boundary": "", "tolerance": ""}],
            "allowed_variation": [{"id": "V01", "variation_dimension": "", "valid_if": "", "minimum_evidence": "", "invalid_if": "", "contrasting_valid_examples": []}],
        },
        "answer_space_tests": [{"id": "AS01", "contrasting_valid_paths": [], "shared_minimum": "", "rubric_expected_result": "", "exclusion_risk": ""}],
        "pitfalls": [{"id": "P01", "observable_failure": "", "why_it_matters": "", "primary_owner": ""}],
        "thresholds": [{"value": "", "purpose": "", "provenance": "prompt | source_natural_denominator | benchmark_convention", "basis_locator": ""}],
        "rejected_constraints": [{"candidate": "", "reason": "not required or not supportable"}],
        "scoring_design": {
            "completion_atoms": [atom],
            "quality_metrics": {
                "content_coverage": [{**atom, "id": "C01"}],
                "accuracy_fidelity": [{**atom, "id": "A01"}],
                "format_compliance": [{**atom, "id": "F01"}],
                "structure_integrity": [{**atom, "id": "S01"}],
                "hallucination_consistency": [{**atom, "id": "H01"}],
            },
            "error_or_cap_triggers": [],
            "stability_keys": [],
        },
        "unresolved_input_gaps": [],
    }
    return json.dumps(value, ensure_ascii=False, indent=2)


def author_analysis(project_dir: Path, task_dir: Path, material_manifest: Path, scratch_dir: Path) -> str:
    packet = _task_input_packet(task_dir, material_manifest)
    return f"""为单道题执行解题式审题，建立内部 authoring record。不要生成 Markdown Rubric 或参考答案。

以下 `AUTHOR_INPUT_JSON` 已包含完整题面和程序生成的素材清单。不要加载 Skill，也不要再次读取题面或素材清单。素材清单列有源素材时，才按其中的 `extracted_artifacts` 精确读取必要内容；素材清单为空时不得扫描目录。实时研究题可使用联网工具实际核验来源。

AUTHOR_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_AUTHOR_INPUT_JSON

临时计算目录（仅确需复算时使用）：{_path(scratch_dir, project_dir)}

权威顺序：当前提示和用户要求 > 问题描述与源素材 > 经核验的版本/时点事实 > 角色方法约束。

{REPORTING_CONTRACT}

要求：
1. 阅读问题与全部源素材，完成至少两个可核验 verification pass。客观或冻结数据题从真实源复算；实时研究题建立来源优先级和时间边界。
2. 主观/混合题建立 objective_verifiable 与 allowed_variation 两层并做答案空间校准；不同但有据的观点、结构、方法或结论均应可得高分。
3. 先拆显式要求，再设计评分。完成率原子只检查任务组成是否完成；质量原子分别衡量覆盖、准确忠实、明确格式义务与可读性、结构关系、编造与内部矛盾。
4. 五项质量都必须可计算，但不得为填满指标新增题面没有的内容义务。题目未规定文件格式时，格式合规只检查可读性、明确披露项可定位性和题面确有的格式义务，不评价审美。
5. 每组权重各自合计 100。ID 命名空间固定：要求 REQ、事实锚点 K、完成率 T、覆盖 C、准确 A、格式 F、结构 S、幻觉 H；不得用 A 同时表示锚点和准确率原子。每个原子只使用 BIN/RATIO/COUNT/CLAIM-RATIO 之一，并写出可计算公式、证据和溯源。CLAIM-RATIO 必须定义主张切分方式、核验全集/抽样规则，并用结构化字段 `empty_set_value`（仅 0 或 1）与 `empty_set_reason` 定义无适用主张的处理；RATIO 的自然分母确实可能为 0 时也必须填写这两个字段，分母不可能为 0 时可保持 null 和空字符串；BIN/COUNT 不得设置空集合策略。
6. 同一缺陷在同一指标内只能有一个主责原子；跨指标影响必须衡量不同后果并明确记录。非题面阈值必须说明 provenance。
7. 每个 mandatory 要求都必须至少落入一个实际计分原子；仅在 `requirement_or_anchor_ids` 中挂名不算落实，状态规则必须直接观察该要求。关系、时点或“洞察明确”等要求，既要检查是否交付，也要在合适的质量原子中操作化正确性或可观察表现。
8. 不得自创“满足 4/5 即满分”等硬阈值；有自然分母时优先按满足项/全部适用项计算 RATIO。事实抽核优先覆盖所有改变结论的主张；其余主张只有在全集过大时才可采用确定性抽样，并记录规则与依据。
9. 锚点必须由所列来源直接支持，不得把其他产品、其他场景或上下文中的信息外推成当前对象事实。单个普通错误或后来失效的链接不得直接触发严重封顶；封顶仅用于有明确证据、会改变结论的实质编造、系统性错误或内部矛盾。历史链接应按任务执行时点和同期证据核验。
10. 对每个实时或外部来源，必须实际打开精确页面并记录完整 `https://` URL、发布方、标题、日期/版本、直接支持内容和核验证据。不得根据 URL 命名规律猜测页面地址。访问受限或暂时不可访问时如实记录状态，并准备官方目录页、存档、第二权威来源或可稳定定位的备用证据；不可访问页面不得单独支撑关键锚点。
11. 内部记录可引用运行目录；最终 Rubric 不得出现 manifest、authoring record、Rubric Spec、schema retry、运行路径或其他内部产物。

只输出：
BEGIN_AUTHORING_RECORD_JSON
{_record_schema()}
END_AUTHORING_RECORD_JSON
"""


def author_spec(project_dir: Path, task_dir: Path, material_manifest: Path, authoring_record: Path) -> str:
    packet = {
        **_task_input_packet(task_dir, material_manifest),
        "authoring_record": _json_file(authoring_record),
    }
    return f"""根据已核验的 authoring record 生成结构化 Rubric 规格；不要撰写 Markdown，Markdown 将由程序确定性渲染。

以下 `SPEC_INPUT_JSON` 是完整输入。不要加载 Skill或读取本地路径；直接依据其中的题面、素材清单和 authoring_record 生成规格。

SPEC_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_SPEC_INPUT_JSON

{REPORTING_CONTRACT}

规格必须完整实现 record 中锁定的原子：ID、输出指标、判据层、权重、状态函数和状态公式逐项一致。只在 `evidence_boundary.dataset_inputs` 中列出题目目录实际存在的 `问题描述.txt` 和真实源素材相对文件名。禁止写入内部 manifest、运行目录、authoring record、审核文件或绝对路径。没有源素材时只列 `问题描述.txt`。

`evidence_boundary.live_sources` 只能包含结构化来源对象，字段必须完整：`id`（SRCnn）、`publisher`、`title`、`date_or_version`、`url`、`supports`、`verification_status`、`verification_evidence`、`fallback_url`、`fallback_note`。`url` 和非空的 `fallback_url` 必须是实际访问过的完整 HTTPS 地址，不得使用裸域名或猜测 URL。仅页面已打开且内容直接支持所述事实时标为 `verified`；访问受限或暂不可用分别标为 `access_restricted`、`temporarily_unavailable`，并填写备用说明，关键锚点还必须有可核验的替代来源。来源表只描述来源及其支持范围，具体事实数值留在锚点表中，不要把多个来源和大量事实拼成一个字符串。确实不依赖外部来源时使用空数组，不得保留 Schema 示例中的占位来源。

任务完成率与五项质量均须有至少一个题目原生原子；不得用通用填充项创造新义务。每组原子权重各自合计 100。所有 mandatory 要求必须被状态规则真实计分，而不是只在引用 ID 中挂名。CLAIM-RATIO 以及分母可能为 0 的 RATIO，其 `empty_set_value` 与 `empty_set_reason` 必须和 record 完全一致；自然分母不得替换成无依据硬阈值。锚点用 K 前缀，准确率原子用 A 前缀。错误规则只保留平均分会掩盖的、会改变结论的实质错误；单个普通错误或后来失效的链接不得触发严重封顶。

只输出严格合法 JSON：
BEGIN_RUBRIC_SPEC_JSON
{json.dumps(rubric_spec_schema_example(), ensure_ascii=False, indent=2)}
END_RUBRIC_SPEC_JSON
"""


def author_record_revision(project_dir: Path, task_dir: Path, material_manifest: Path, current_record: Path, current_spec: Path, review_file: Path, round_number: int) -> str:
    packet = {
        **_task_input_packet(task_dir, material_manifest),
        "current_record": _json_file(current_record),
        "current_spec": _json_file(current_spec),
        "review": _json_file(review_file),
    }
    return f"""只修订 authoring record。这是第 {round_number} 轮；不要输出 Rubric 规格或 Markdown。

以下 `REVISION_INPUT_JSON` 是完整输入。不要加载 Skill或读取本地路径；直接完成修订。只有 blocking 明确要求重新核验实时来源时才调用联网工具。

REVISION_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_REVISION_INPUT_JSON

只处理 blocking；拒绝无题面或素材依据的新义务。必须逐项核对 blocking 指向的状态规则和来源，不得用增加引用 ID 代替修复可执行判据。除非 blocking 证明评分契约错误，否则保留稳定 ID、指标归属、层、权重、状态函数和公式。{REPORTING_CONTRACT}

BEGIN_AUTHORING_RECORD_JSON
{_record_schema()}
END_AUTHORING_RECORD_JSON
"""


def author_spec_revision(project_dir: Path, task_dir: Path, material_manifest: Path, revised_record: Path, previous_spec: Path, review_file: Path, round_number: int) -> str:
    packet = {
        **_task_input_packet(task_dir, material_manifest),
        "revised_record": _json_file(revised_record),
        "previous_spec": _json_file(previous_spec),
        "review": _json_file(review_file),
    }
    return f"""根据修订后的 authoring record 输出第 {round_number} 轮完整 Rubric 规格；不要输出 Markdown。

以下 `SPEC_REVISION_INPUT_JSON` 是完整输入。不要加载 Skill或读取本地路径；直接输出修订规格。

SPEC_REVISION_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_SPEC_REVISION_INPUT_JSON

只落实 blocking 与修订后 record。保持固定六项接口和自包含要求；再次检查 mandatory 要求真实计分、CLAIM-RATIO 与可能出现零分母的 RATIO 空集合、阈值来源、K/A 命名空间及严重封顶的实质性条件。只输出：
BEGIN_RUBRIC_SPEC_JSON
{json.dumps(rubric_spec_schema_example(), ensure_ascii=False, indent=2)}
END_RUBRIC_SPEC_JSON
"""


def review(project_dir: Path, task_dir: Path, material_manifest: Path, authoring_record: Path, spec_file: Path, rubric_file: Path, round_number: int, previous_review: Path | None = None, arbitration_file: Path | None = None, final_verification: bool = False) -> str:
    packet = {
        **_task_input_packet(task_dir, material_manifest),
        "authoring_record": _json_file(authoring_record),
        "rubric_spec": _json_file(spec_file),
        "rendered_rubric": rubric_file.read_text(encoding="utf-8"),
        "previous_review": _json_file(previous_review) if previous_review else None,
        "arbitration": _json_file(arbitration_file) if arbitration_file else None,
    }
    mode = "最终锁定版核查" if final_verification else ("裁决落实核查" if arbitration_file else "独立审核")
    review_scope = (
        "这是首次审核：执行完整题意、评分合同与来源核验。"
        if round_number == 1 else
        "这是修改后的复核：只验证上一轮 blocking 是否落实、变化内容是否引入实质回归。"
        "若来源条目、URL、支持范围和锚点均未变化，复用首次审核的核验结论，不重复打开全部来源；"
        "只有来源发生变化或上一轮 blocking 涉及来源时才重新核验相应页面。"
    )
    return f"""对现有结构化评分设计做{mode}。你是审核人，不是第二个制作人。轮次：{round_number}。

{review_scope}

以下 `REVIEW_INPUT_JSON` 已包含题面、素材清单和全部候选产物。不要加载 Skill或重复读取这些本地文件。仅在首次核验实时来源、来源发生变化或 blocking 涉及来源时调用联网工具。

REVIEW_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_REVIEW_INPUT_JSON

检查：题意和锚点；时间公平；完成率与质量是否分离；六项结果是否各自可计算；五项质量是否题目原生且无填充义务；原子与 record 是否一致；分母、空集合、零分条件和证据是否明确；同一指标内是否重复扣分；开放结论是否被错误收窄；最终 Rubric 是否只引用数据集真实输入或外部来源。逐项检查外部来源是否为结构化独立条目、是否使用完整可点击 HTTPS URL、页面是否实际打开且直接支持声明；不可访问来源是否有替代证据且没有单独支撑关键锚点。程序固定章节的措辞或样式偏好不得 blocking。

以下属于 blocking，不得降为 suggestion：mandatory 要求只挂引用 ID 而状态规则未观察；CLAIM-RATIO 缺空集合/全集规则；RATIO 的分母可能为 0 却未定义空集合结果；无来源的硬阈值；record 与 spec 的指标归属、权重、状态函数或引用关系存在会改变评分的语义冲突；内部构建产物泄漏；外部来源使用裸域名、猜测地址或虚假 `verified` 状态；不可访问来源无替代证据却单独支撑关键锚点；以当前链接失效惩罚历史交付；来源不能直接支持锚点；单个普通错误触发与实质性不相称的严重封顶。record 与 spec 的同义改写、解释详略或非评分措辞差异不是 blocking。审核时必须引用并检查原子的精确状态规则，不得根据标题、锚点说明或自己的推断替它补足。

只有会导致错误评分、不可复算、偏离题意、非自包含或违反裁决的问题才能 blocking。只输出审核 Agent 约定的 BEGIN_REVIEW_JSON / END_REVIEW_JSON JSON。
"""


def classify_review(
    project_dir: Path,
    task_dir: Path,
    authoring_record: Path,
    spec_file: Path,
    rubric_file: Path,
    raw_review_file: Path,
) -> str:
    packet = {
        "problem": (task_dir / "问题描述.txt").read_text(encoding="utf-8-sig"),
        "authoring_record": _json_file(authoring_record),
        "rubric_spec": _json_file(spec_file),
        "rendered_rubric": rubric_file.read_text(encoding="utf-8"),
        "raw_review": _json_file(raw_review_file),
    }
    return f"""对 Reviewer 已写入 suggestions 的项目执行独立严重性分类门禁。不要重新审核整份 Rubric，不得发现或新增原审核未提到的问题。

以下 `REVIEW_GATE_INPUT_JSON` 是完整输入。不得加载 Skill、读取路径或调用其他工具。

REVIEW_GATE_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_REVIEW_GATE_INPUT_JSON

逐条读取 suggestions。只有在现有 Rubric 文本中能够给出一个具体交付情形，并证明该情形必然导致错误原子状态、明确分数差、错误封顶或两名评卷人按现有规则得出不同结果时，才可提升。必须在 evidence/reason 中写清具体反例和受影响的原子；“可能”“建议进一步明确”“存在部分重叠”或纯推测不足以提升。

以下情况在证据充分时必须提升为 blocking：mandatory 要求未被状态规则完整观察；同一指标内同一具体缺陷确定会被重复扣分；公式、分母、空集合、N/A、容差或时间分支不可计算；锚点的事实、日期、币种、口径或来源冲突；使用无依据硬阈值；缺少判分所必需的锚点；最终 Rubric 依赖或泄漏内部构建/运行环境；外部来源为裸域名、猜测 URL、虚假 `verified` 或不可访问且无备用证据；合法答案会被错误扣分。若现有规则已经给出可机械执行的职责分工，只是还可写得更清楚，则保留为 suggestion。

只有纯措辞润色、非必要可读性提升、不会改变任何状态或得分的优化才能保留为 suggestion。不要提升审美偏好或无证据猜测。

只输出：
BEGIN_REVIEW_GATE_JSON
{{
  "verdict": "classification_passed 或 promotion_required",
  "summary": "分类结论",
  "promoted": [
    {{
      "suggestion_index": 1,
      "id": "G001",
      "category": "task_fidelity、anchor、variation_fairness、scope、determinism、evidence、revision_application 之一",
      "location": "可定位位置",
      "evidence": "原 suggestion 与 Rubric 中支持提升的证据",
      "reason": "为何会改变评分或破坏可复算性",
      "required_change": "可验证的修复条件"
    }}
  ]
}}
END_REVIEW_GATE_JSON
"""


def arbitrate(project_dir: Path, task_dir: Path, material_manifest: Path, current_record: Path, current_spec: Path, record_history: list[Path], spec_history: list[Path], review_files: list[Path]) -> str:
    record = json.loads(current_record.read_text(encoding="utf-8"))
    spec = json.loads(current_spec.read_text(encoding="utf-8"))
    reviews = [json.loads(path.read_text(encoding="utf-8")) for path in review_files]
    packet = {
        "problem": (task_dir / "问题描述.txt").read_text(encoding="utf-8-sig"),
        "material_manifest": json.loads(material_manifest.read_text(encoding="utf-8")),
        "latest_record": {
            key: record.get(key)
            for key in (
                "requirements", "anchors", "evaluation_layers", "answer_space_tests",
                "pitfalls", "thresholds", "rejected_constraints", "scoring_design",
                "unresolved_input_gaps",
            )
        },
        "latest_spec": {
            key: spec.get(key)
            for key in (
                "task_objective", "evidence_boundary", "requirements", "anchors",
                "allowed_variations", "completion", "quality_metrics", "error_rules",
                "unresolved_input_gaps",
            )
        },
        "reviews": reviews,
        "history": {
            "record_versions": len(record_history),
            "spec_versions": len(spec_history),
            "review_versions": len(review_files),
        },
    }
    return f"""对一轮修改后仍未解决的结构化 Rubric 分歧作一次绑定裁决。

以下 `ARBITRATION_INPUT_JSON` 已由程序从通过校验的检查点生成，是本次裁决的完整输入。不得调用工具或读取路径；直接根据其中的题面、锚点、评分合同和审核记录裁决。

ARBITRATION_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_ARBITRATION_INPUT_JSON

以题目、真实素材和已核验锚点为准。拒绝无依据的新义务、阈值和审美偏好；固定六项输出接口不得改变。只输出约定的 BEGIN_ARBITRATION_JSON / END_ARBITRATION_JSON JSON。
"""


def author_final_patch(project_dir: Path, task_dir: Path, material_manifest: Path, current_record: Path, current_spec: Path, arbitration_file: Path, compliance_review: Path | None = None) -> str:
    # Finalization operates only on artifacts that have already passed schema
    # validation.  Embed compact JSON instead of asking the agent to load the
    # skill and several large files in separate tool turns.  Some OpenAI-
    # compatible gateways reject the enlarged follow-up request even though
    # the same material fits comfortably in one request.
    packet = {
        "current_record": json.loads(current_record.read_text(encoding="utf-8")),
        "current_spec": json.loads(current_spec.read_text(encoding="utf-8")),
        "arbitration": json.loads(arbitration_file.read_text(encoding="utf-8")),
        "compliance_review": (
            json.loads(compliance_review.read_text(encoding="utf-8"))
            if compliance_review else None
        ),
    }
    return f"""根据绑定裁决对当前 authoring record 与 Rubric Spec 生成最小 JSON Patch；不要重写完整对象，不要输出 Markdown。

以下 `FINALIZATION_INPUT_JSON` 是程序从已通过校验的检查点生成的完整输入。不得调用工具、加载 Skill 或读取任何路径；直接对其中的 current_record 与 current_spec 生成补丁。

FINALIZATION_INPUT_JSON
{json.dumps(packet, ensure_ascii=False, separators=(",", ":"))}
END_FINALIZATION_INPUT_JSON

要求：
1. 落实 arbitration 中 decision=accept/modify 的 binding_change 与 final_instructions，以及 compliance_review 中的 blocking；不得落实被 reject 的意见。
2. 使用 RFC 6902 风格操作，op 只允许 add、remove、replace；path 使用标准 JSON Pointer。
3. value 只放被修改字段或节点的新值，不得把整个 record/spec 塞进补丁。
4. record_patch 与 spec_patch 都必须是数组；无需修改时输出空数组。
5. 补丁应用后，record 与 spec 的评分原子语义必须逐字对齐，固定六项输出接口不得改变。

只输出：
BEGIN_FINAL_PATCH_JSON
{{
  "record_patch": [{{"op": "replace", "path": "/JSON/Pointer", "value": "新值"}}],
  "spec_patch": [{{"op": "replace", "path": "/JSON/Pointer", "value": "新值"}}]
}}
END_FINAL_PATCH_JSON
"""


def author_final_spec(project_dir: Path, task_dir: Path, material_manifest: Path, finalized_record: Path, current_spec: Path, arbitration_file: Path, compliance_review: Path | None = None) -> str:
    compliance = _path(compliance_review, project_dir) if compliance_review else "无"
    return f"""根据锁定 record 与绑定裁决输出完整最终 Rubric 规格；不要输出 Markdown。

题目目录：{_path(task_dir, project_dir)}
内部素材清单：{_path(material_manifest, project_dir)}
锁定 record：{_path(finalized_record, project_dir)}
当前规格：{_path(current_spec, project_dir)}
裁决：{_path(arbitration_file, project_dir)}
裁决落实复核：{compliance}

保持固定六项接口、自包含要求和 record 原子契约。只输出：
BEGIN_RUBRIC_SPEC_JSON
{json.dumps(rubric_spec_schema_example(), ensure_ascii=False, indent=2)}
END_RUBRIC_SPEC_JSON
"""
