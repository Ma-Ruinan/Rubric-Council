from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rubric_generator.audit import (
    apply_review_gate,
    audit_authoring_record,
    audit_file,
    audit_review_gate_payload,
)
from rubric_generator.config import DEFAULT_MODEL, load_project_env, load_project_settings
from rubric_generator.cli import build_parser
from rubric_generator.dataset import discover_tasks
from rubric_generator.opencode_client import AgentResponse, OpenCodeClient, parse_event_stream
from rubric_generator.rubric_spec import (
    METRICS,
    audit_authoring_record_spec_alignment,
    audit_rubric_spec,
    normalize_rubric_spec,
    render_rubric,
)
from rubric_generator.runner import (
    RubricRun,
    RunOptions,
    apply_json_patch,
    audit_final_patch_payload,
)


def planned_atom(atom_id: str, metric: str) -> dict[str, object]:
    return {
        "id": atom_id,
        "evaluation_layer": "objective_verifiable",
        "primary_purpose": f"核验 {metric}",
        "weight": 100,
        "state_function": "BIN",
        "formula_or_state_rule": "找到指定证据且满足要求时为 1，否则为 0",
        "empty_set_value": None,
        "empty_set_reason": "",
        "requirement_or_anchor_ids": ["REQ01"],
    }


def sample_record() -> dict[str, object]:
    return {
        "task_type": "objective_frozen",
        "judgment_profile": "objective",
        "evidence_boundary": {
            "frozen": ["问题描述.txt"], "live": [],
            "cutoff_or_version": "以题面为准", "precedence": ["题面"],
        },
        "requirements": [{
            "id": "REQ01", "text": "提交一份结果", "source": "problem",
            "locator": "问题描述.txt:1", "mandatory": True, "allowed_variation": "措辞可变",
        }],
        "materials_reviewed": [{"path": "问题描述.txt", "sha256": "x", "inspection": "全文"}],
        "verification_passes": [
            {"id": "VP01", "purpose": "拆题", "method_or_command": "read", "source_locators": ["问题描述.txt:1"], "result": "一项要求", "cross_check": "复读"},
            {"id": "VP02", "purpose": "复核", "method_or_command": "compare", "source_locators": ["问题描述.txt:1"], "result": "一致", "cross_check": "独立核对"},
        ],
        "anchors": [],
        "evaluation_layers": {
            "objective_verifiable": [{
                "id": "O01", "judgment": "是否提交", "requirement_or_anchor_ids": ["REQ01"],
                "verification_rule": "查找交付", "expected_value_or_boundary": "存在", "tolerance": "无",
            }],
            "allowed_variation": [],
        },
        "answer_space_tests": [],
        "pitfalls": [],
        "thresholds": [],
        "rejected_constraints": [],
        "scoring_design": {
            "completion_atoms": [planned_atom("T01", "completion")],
            "quality_metrics": {
                key: [planned_atom(f"{prefix}01", key)] for key, _, prefix in METRICS
            },
            "error_or_cap_triggers": [],
            "stability_keys": ["REQ01"],
        },
        "unresolved_input_gaps": [],
    }


def sample_spec() -> dict[str, object]:
    def atom(atom_id: str, metric: str) -> dict[str, object]:
        return {
            "id": atom_id,
            "layer": "objective_verifiable",
            "weight": 100,
            "primary_purpose": f"核验 {metric}",
            "state_function": "BIN",
            "state_rule": "找到指定证据且满足要求时为 1，否则为 0",
            "empty_set_value": None,
            "empty_set_reason": "",
            "required_evidence": "交付文件中的可定位内容",
            "requirement_or_anchor_ids": ["REQ01"],
        }

    return {
        "schema_version": "1.0",
        "title": "样例任务",
        "task_objective": "核验交付是否满足题面要求。",
        "evidence_boundary": {
            "dataset_inputs": [{"file": "问题描述.txt", "role": "题面"}],
            "live_sources": [], "cutoff_rule": "以执行时点为准", "source_precedence": ["题面"],
        },
        "delivery_interpretation": ["任一可读文本文件均可。"],
        "requirements": [{
            "id": "REQ01", "text": "提交一份结果", "locator": "问题描述.txt:1",
            "mandatory": True, "allowed_variation": "措辞可变",
        }],
        "anchors": [],
        "allowed_variations": [],
        "completion": {"atoms": [atom("T01", "completion")]},
        "quality_metrics": {
            key: {"atoms": [atom(f"{prefix}01", key)]} for key, _, prefix in METRICS
        },
        "error_rules": [],
        "evidence_record_requirements": ["记录文件名和段落定位。"],
        "unresolved_input_gaps": [],
    }


class RubricSpecTests(unittest.TestCase):
    def test_valid_spec_renders_fixed_reporting_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            (task / "问题描述.txt").write_text("提交一份结果", encoding="utf-8")
            spec = sample_spec()
            self.assertEqual(audit_rubric_spec(spec, task), [])
            self.assertEqual(audit_authoring_record_spec_alignment(sample_record(), spec), [])
            text = render_rubric(spec)
            self.assertIn("任务完成率 =", text)
            self.assertIn("0.00%—100.00%", text)
            self.assertIn("### 7.5 幻觉／自洽性", text)
            self.assertIn("0.00—5.00", text)
            rubric = task / "rubric.md"
            rubric.write_text(text, encoding="utf-8")
            self.assertTrue(audit_file(rubric).passed)

    def test_internal_or_missing_input_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            (task / "问题描述.txt").write_text("x", encoding="utf-8")
            spec = sample_spec()
            spec["evidence_boundary"]["dataset_inputs"].append(
                {"file": "materials/material-manifest.json", "role": "内部文件"}
            )
            issues = audit_rubric_spec(spec, task)
            self.assertTrue(any("internal" in issue for issue in issues))
            self.assertTrue(any("does not exist" in issue for issue in issues))

    def test_live_sources_are_structured_and_render_clickable_https_links(self):
        spec = sample_spec()
        spec["evidence_boundary"]["live_sources"] = [{
            "id": "SRC01", "publisher": "示例机构", "title": "官方数据页",
            "date_or_version": "持续更新", "url": "https://www.iana.org/domains/reserved",
            "supports": "核验示例指标", "verification_status": "verified",
            "verification_evidence": "已打开页面并定位到数据表", "fallback_url": "",
            "fallback_note": "",
        }]
        self.assertEqual(audit_rubric_spec(spec), [])
        rendered = render_rubric(spec)
        self.assertIn("| SRC01 | 示例机构 | 官方数据页 |", rendered)
        self.assertIn("<https://www.iana.org/domains/reserved>", rendered)

    def test_bare_or_unstructured_live_source_is_blocked(self):
        spec = sample_spec()
        spec["evidence_boundary"]["live_sources"] = ["example.com/data"]
        self.assertIn("live source 1 is not an object", audit_rubric_spec(spec))
        spec["evidence_boundary"]["live_sources"] = [{
            "id": "SRC01", "publisher": "示例机构", "title": "数据页",
            "date_or_version": "2026-09", "url": "example.com/data",
            "supports": "指标", "verification_status": "temporarily_unavailable",
            "verification_evidence": "请求失败", "fallback_url": "",
            "fallback_note": "",
        }]
        issues = audit_rubric_spec(spec)
        self.assertIn("live source SRC01 requires an absolute HTTPS URL", issues)
        self.assertIn("live source SRC01 requires fallback_note when not verified", issues)

    def test_each_score_group_must_sum_to_one_hundred(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            (task / "问题描述.txt").write_text("x", encoding="utf-8")
            spec = sample_spec()
            spec["quality_metrics"]["content_coverage"]["atoms"][0]["weight"] = 80
            issues = audit_rubric_spec(spec, task)
            self.assertIn("content_coverage atom weights must sum to 100, got 80", issues)

    def test_record_and_spec_alignment_is_enforced(self):
        spec = sample_spec()
        spec["completion"]["atoms"][0]["state_function"] = "RATIO"
        issues = audit_authoring_record_spec_alignment(sample_record(), spec)
        self.assertIn("atom T01 differs on state_function", issues)

    def test_record_and_spec_reference_alignment_is_enforced(self):
        spec = sample_spec()
        spec["completion"]["atoms"][0]["requirement_or_anchor_ids"] = ["REQ02"]
        issues = audit_authoring_record_spec_alignment(sample_record(), spec)
        self.assertIn("atom T01 differs on requirement_or_anchor_ids", issues)

    def test_record_and_spec_wording_differences_do_not_block(self):
        spec = sample_spec()
        atom = spec["completion"]["atoms"][0]
        atom["primary_purpose"] = "以另一种表述核验交付"
        atom["state_rule"] = "交付中存在可定位证据则为 1，否则为 0"
        atom["empty_set_reason"] = "非 CLAIM-RATIO，不适用"
        self.assertEqual(audit_authoring_record_spec_alignment(sample_record(), spec), [])

    def test_presentation_variants_are_normalized_without_changing_renderer(self):
        spec = sample_spec()
        spec["delivery_interpretation"] = [{"文件": "可读", "格式": "不限"}]
        spec["evidence_boundary"]["live_sources"] = [{
            "id": "SRC01", "publisher": "示例机构", "title": "旧版数据页",
            "date_or_version": "历史版本", "url": "https://www.iana.org/domains/reserved",
            "supports": "历史口径", "verification_status": "verified_but_superseded",
            "verification_evidence": "页面已打开；该版本已被后续版本替代",
            "fallback_url": "", "fallback_note": "",
        }]
        changes = normalize_rubric_spec(spec)
        self.assertTrue(changes)
        self.assertEqual(spec["evidence_boundary"]["live_sources"][0]["verification_status"], "verified")
        self.assertEqual(spec["delivery_interpretation"], ["文件：可读；格式：不限"])
        self.assertEqual(audit_rubric_spec(spec), [])
        self.assertIn("## 10. 未解决输入缺口与判定边界", render_rubric(spec))

    def test_internal_reference_with_spaces_is_blocked(self):
        spec = sample_spec()
        spec["unresolved_input_gaps"] = ["详见 authoring record 中的分析"]
        issues = audit_rubric_spec(spec)
        self.assertTrue(any("internal build reference" in issue for issue in issues))

    def test_claim_ratio_requires_empty_case(self):
        spec = sample_spec()
        atom = spec["quality_metrics"]["accuracy_fidelity"]["atoms"][0]
        atom["state_function"] = "CLAIM-RATIO"
        atom["state_rule"] = "正确主张数/全部主张数"
        issues = audit_rubric_spec(spec)
        self.assertIn("A01 CLAIM-RATIO empty_set_value must be 0 or 1", issues)
        self.assertIn("A01 CLAIM-RATIO requires empty_set_reason", issues)

    def test_claim_ratio_uses_structured_empty_case(self):
        spec = sample_spec()
        atom = spec["quality_metrics"]["accuracy_fidelity"]["atoms"][0]
        atom["state_function"] = "CLAIM-RATIO"
        atom["state_rule"] = "正确主张数/全部主张数"
        atom["empty_set_value"] = 0
        atom["empty_set_reason"] = "没有可核验主张时缺少忠实度证据"
        self.assertEqual(audit_rubric_spec(spec), [])
        rendered = render_rubric(spec)
        self.assertIn("空集合状态=0", rendered)

    def test_ratio_may_define_empty_case_when_denominator_can_be_zero(self):
        spec = sample_spec()
        atom = spec["quality_metrics"]["accuracy_fidelity"]["atoms"][0]
        atom["state_function"] = "RATIO"
        atom["state_rule"] = "正确条目数/全部适用条目数"
        atom["empty_set_value"] = 1
        atom["empty_set_reason"] = "没有适用条目时不存在该类错误"
        self.assertEqual(audit_rubric_spec(spec), [])
        self.assertIn("空集合状态=1", render_rubric(spec))

    def test_ratio_rejects_partial_empty_case_policy(self):
        spec = sample_spec()
        atom = spec["quality_metrics"]["accuracy_fidelity"]["atoms"][0]
        atom["state_function"] = "RATIO"
        atom["state_rule"] = "正确条目数/全部适用条目数"
        atom["empty_set_value"] = 1
        issues = audit_rubric_spec(spec)
        self.assertTrue(any("RATIO empty-set policy requires" in issue for issue in issues))

    def test_bin_still_rejects_empty_case_policy(self):
        spec = sample_spec()
        atom = spec["quality_metrics"]["accuracy_fidelity"]["atoms"][0]
        atom["empty_set_value"] = 1
        atom["empty_set_reason"] = "不应允许"
        issues = audit_rubric_spec(spec)
        self.assertTrue(any("BIN atom must not define" in issue for issue in issues))


class ReviewClassificationGateTests(unittest.TestCase):
    def test_score_changing_suggestion_is_promoted(self):
        review = {
            "verdict": "passed", "summary": "初审通过", "blocking": [],
            "verified": ["结构可解析"],
            "suggestions": ["REQ09 未检查截止日期是否与实际执行时间一致"],
        }
        gate = {
            "verdict": "promotion_required", "summary": "会改变得分",
            "promoted": [{
                "suggestion_index": 1, "id": "G001", "category": "task_fidelity",
                "location": "REQ09/T05", "evidence": "T05 仅检查日期存在",
                "reason": "错误日期不会被扣分", "required_change": "状态规则核验执行时间",
            }],
        }
        self.assertEqual(audit_review_gate_payload(gate, 1), [])
        merged = apply_review_gate(review, gate)
        self.assertEqual(merged["verdict"], "revision_required")
        self.assertEqual(len(merged["blocking"]), 1)
        self.assertEqual(merged["suggestions"], [])


class AuthoringRecordAuditTests(unittest.TestCase):
    def test_unknown_anchor_reference_is_blocked(self):
        record = sample_record()
        record["scoring_design"]["quality_metrics"]["accuracy_fidelity"][0][
            "requirement_or_anchor_ids"
        ] = ["K99"]
        issues = audit_authoring_record(record)
        self.assertTrue(any("references unknown requirements or anchors: K99" in issue for issue in issues))

    def test_anchor_namespace_is_distinct_from_accuracy_atoms(self):
        record = sample_record()
        record["anchors"] = [{
            "id": "A01", "type": "mandatory_fact", "value_or_proposition": "事实",
            "source": "题面", "locator": "问题描述.txt:1", "verification_method": "复核",
            "verification_result": "确认", "tolerance": "无", "confidence_boundary": "确定",
        }]
        issues = audit_authoring_record(record)
        self.assertIn("anchor 1 ID must use Knn prefix", issues)

    def test_mandatory_requirement_must_be_scored(self):
        record = deepcopy(sample_record())
        record["requirements"].append({
            "id": "REQ02", "text": "给出截止日期", "source": "problem",
            "locator": "问题描述.txt:2", "mandatory": True, "allowed_variation": "日期格式可变",
        })
        issues = audit_authoring_record(record)
        self.assertIn("mandatory requirements have no scored atom: REQ02", issues)


class ConfigurationTests(unittest.TestCase):
    def test_resume_accepts_missing_only_filter(self):
        args = build_parser().parse_args([
            "resume", "--dataset", "dataset", "--run-id", "run", "--missing-only",
        ])
        self.assertTrue(args.missing_only)

    def test_defaults_and_per_role_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = load_project_settings(root)
            self.assertTrue(all(model == DEFAULT_MODEL for model in defaults.agent_models.values()))
            (root / "rubric-generator.toml").write_text(
                '[agents.rubric-reviewer]\nmodel = "aiaaa/another-model#high"\n', encoding="utf-8"
            )
            settings = load_project_settings(root)
            self.assertEqual(settings.agent_models["rubric-reviewer"], "aiaaa/another-model#high")
            self.assertEqual(settings.agent_models["rubric-author"], DEFAULT_MODEL)
            self.assertEqual(settings.agent_models["rubric-finalizer"], DEFAULT_MODEL)
            self.assertEqual(settings.agent_models["rubric-runtime-probe"], DEFAULT_MODEL)

    def test_project_env_loads_without_overwriting_process_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "RUBRIC_TEST_NEW=from-file\nRUBRIC_TEST_EXISTING=from-file\n", encoding="utf-8"
            )
            previous_new = os.environ.pop("RUBRIC_TEST_NEW", None)
            previous_existing = os.environ.get("RUBRIC_TEST_EXISTING")
            os.environ["RUBRIC_TEST_EXISTING"] = "from-process"
            try:
                loaded = load_project_env(root)
                self.assertIn("RUBRIC_TEST_NEW", loaded)
                self.assertNotIn("RUBRIC_TEST_EXISTING", loaded)
                self.assertEqual(os.environ["RUBRIC_TEST_NEW"], "from-file")
                self.assertEqual(os.environ["RUBRIC_TEST_EXISTING"], "from-process")
            finally:
                os.environ.pop("RUBRIC_TEST_NEW", None)
                if previous_new is not None:
                    os.environ["RUBRIC_TEST_NEW"] = previous_new
                if previous_existing is None:
                    os.environ.pop("RUBRIC_TEST_EXISTING", None)
                else:
                    os.environ["RUBRIC_TEST_EXISTING"] = previous_existing


class OpenCodeClientTests(unittest.TestCase):
    def test_incomplete_provider_error_retries_whole_agent_call(self):
        failed = "\n".join([
            json.dumps({"type": "text", "part": {"text": "I will read inputs."}}),
            json.dumps({"type": "error", "error": {
                "type": "provider.invalid-request", "message": "HTTP 400",
            }}),
        ]) + "\n"
        succeeded = json.dumps({
            "type": "text",
            "part": {"text": "BEGIN_TEST\n{\"ok\": true}\nEND_TEST"},
        }) + "\n"
        completed = [
            SimpleNamespace(returncode=1, stdout=failed, stderr=""),
            SimpleNamespace(returncode=0, stdout=succeeded, stderr=""),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch(
            "rubric_generator.opencode_client.subprocess.run", side_effect=completed
        ) as mocked_run, patch(
            "rubric_generator.opencode_client.time.sleep"
        ), patch(
            "rubric_generator.opencode_client._resolve_opencode_executable", return_value="opencode"
        ):
            client = OpenCodeClient(Path(tmp), retries=1)
            response = client.run_agent("rubric-author", "prompt", "title")
            self.assertIn("BEGIN_TEST", response.text)
            self.assertEqual(mocked_run.call_count, 2)

    def test_nonzero_exit_with_complete_model_output_is_validated_by_caller(self):
        event = json.dumps({
            "type": "text",
            "part": {"text": "BEGIN_TEST\n{\"ok\": true}\nEND_TEST"},
        })
        completed = SimpleNamespace(returncode=1, stdout=event + "\n", stderr="gateway closed late")
        with tempfile.TemporaryDirectory() as tmp, patch(
            "rubric_generator.opencode_client.subprocess.run", return_value=completed
        ), patch(
            "rubric_generator.opencode_client._resolve_opencode_executable", return_value="opencode"
        ):
            client = OpenCodeClient(Path(tmp), retries=0)
            response = client.run_agent("rubric-reviewer", "prompt", "title")
            self.assertIn("BEGIN_TEST", response.text)

    def test_provider_error_is_retained_separately_from_preamble_text(self):
        raw = "\n".join([
            json.dumps({"type": "text", "part": {"text": "I will read inputs."}}),
            json.dumps({"type": "error", "error": {
                "type": "provider.invalid-request", "message": "HTTP 400",
            }}),
        ])
        response = parse_event_stream(raw)
        self.assertEqual(response.text, "I will read inputs.")
        self.assertEqual(response.terminal_error, "provider.invalid-request: HTTP 400")

    def test_long_prompt_is_passed_as_attached_file_on_windows_safe_command(self):
        event = json.dumps({
            "type": "text",
            "part": {"text": "BEGIN_TEST\n{\"ok\": true}\nEND_TEST"},
        })
        completed = SimpleNamespace(returncode=0, stdout=event + "\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp, patch(
            "rubric_generator.opencode_client.subprocess.run", return_value=completed
        ) as mocked_run, patch(
            "rubric_generator.opencode_client._resolve_opencode_executable", return_value="opencode"
        ):
            client = OpenCodeClient(Path(tmp), retries=0)
            long_prompt = "长" * 20_000
            client.run_agent("rubric-arbitrator", long_prompt, "long-prompt")
            command = mocked_run.call_args.args[0]
            self.assertIn("--file", command)
            self.assertNotIn(long_prompt, command)
            prompt_path = Path(tmp) / command[command.index("--file") + 1]
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), long_prompt)


class FakeClient:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)

    def run_agent(self, agent: str, prompt: str, title: str) -> AgentResponse:
        text = next(self.responses)
        event = json.dumps({"type": "text", "part": {"text": text}}, ensure_ascii=False)
        return AgentResponse(text=text, session_ids=("fake",), raw_output=event + "\n")


class RunnerTests(unittest.TestCase):
    def test_default_allows_only_one_reviewer_directed_revision(self):
        self.assertEqual(RunOptions().max_revision_rounds, 1)

    def test_final_patch_is_small_and_applies_locally(self):
        document = {"metrics": [{"id": "S01", "rule": "old"}], "fixed": 100}
        payload = {
            "record_patch": [],
            "spec_patch": [{
                "op": "replace", "path": "/metrics/0/rule", "value": "new",
            }],
        }
        self.assertEqual(audit_final_patch_payload(payload), [])
        patched = apply_json_patch(deepcopy(document), payload["spec_patch"])
        self.assertEqual(patched["metrics"][0]["rule"], "new")
        self.assertEqual(patched["fixed"], 100)

    def test_final_patch_rejects_root_replacement(self):
        with self.assertRaises(ValueError):
            apply_json_patch({"kept": True}, [{"op": "replace", "path": "", "value": {}}])

    def test_resume_repairs_complete_but_locally_damaged_json_response(self):
        begin = "BEGIN_TEST_JSON"
        end = "END_TEST_JSON"
        damaged = (
            f"{begin}\n"
            '{"items":[{"id":"A",\n'
            '"text":"truncated\n'
            '"text":"complete"}]}\n'
            f"{end}\n"
        )
        repaired = f'{begin}\n{{"items":[{{"id":"A","text":"complete"}}]}}\n{end}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            dataset = root / "dataset"
            project.mkdir()
            dataset.mkdir()
            run = RubricRun(
                project, dataset, RunOptions(resume=True, retries=0), run_id="repair-test"
            )
            run.client = FakeClient([repaired])
            artifacts = run.run_dir / "tasks" / "task" / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "phase.response.txt").write_text(damaged, encoding="utf-8")
            payload = run._call_json_agent(
                "task", artifacts, "phase", "rubric-author", "unused", "unused",
                begin, end,
            )
            self.assertEqual(payload["items"][0]["text"], "complete")
            self.assertTrue((artifacts / "phase.json-repair.local.json").is_file())

    def test_offline_full_loop_writes_only_rubric_to_task(self):
        record = sample_record()
        self.assertEqual(audit_authoring_record(record), [])
        spec = sample_spec()
        review = {"verdict": "passed", "summary": "可用", "blocking": [], "verified": ["通过"], "suggestions": []}
        responses = [
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            dataset = root / "dataset"
            task_dir = dataset / "维度一" / "题目一"
            project.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (task_dir / "问题描述.txt").write_text("提交一份结果", encoding="utf-8")
            run = RubricRun(project, dataset, RunOptions(concurrency=1, retries=0), run_id="offline-test")
            run.client = FakeClient(responses)
            outcomes = run.execute(discover_tasks(dataset))
            self.assertEqual(outcomes[0].status, "COMPLETED")
            self.assertEqual(sorted(path.name for path in task_dir.iterdir()), ["rubric.md", "问题描述.txt"])
            artifacts = project / ".rubric-generator" / "runs" / "offline-test" / "tasks"
            self.assertTrue(any(artifacts.rglob("authoring-record-00.json")))
            self.assertTrue(any(artifacts.rglob("rubric-spec-00.json")))

    def test_review_gate_promotion_triggers_automatic_revision(self):
        record = sample_record()
        spec = sample_spec()
        raw_review = {
            "verdict": "passed", "summary": "初审通过", "blocking": [],
            "verified": ["结构可解析"],
            "suggestions": ["强制日期要求没有进入状态规则，会改变得分"],
        }
        gate = {
            "verdict": "promotion_required", "summary": "建议实际影响评分",
            "promoted": [{
                "suggestion_index": 1, "id": "G001", "category": "task_fidelity",
                "location": "REQ01/T01", "evidence": "状态规则未观察完整要求",
                "reason": "会使错误交付得分不变", "required_change": "补全可观察判据",
            }],
        }
        passed_review = {
            "verdict": "passed", "summary": "修改后可用", "blocking": [],
            "verified": ["已落实"], "suggestions": [],
        }
        responses = [
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(raw_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_REVIEW_GATE_JSON\n" + json.dumps(gate, ensure_ascii=False) + "\nEND_REVIEW_GATE_JSON",
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(passed_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            task_dir = root / "dataset" / "维度一" / "题目一"
            project.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (task_dir / "问题描述.txt").write_text("提交一份结果", encoding="utf-8")
            run = RubricRun(project, root / "dataset", RunOptions(concurrency=1, retries=0), run_id="gate-test")
            run.client = FakeClient(responses)
            outcomes = run.execute(discover_tasks(root / "dataset"))
            self.assertEqual(outcomes[0].status, "COMPLETED")
            self.assertEqual(outcomes[0].revision_rounds, 1)
            artifacts = project / ".rubric-generator" / "runs" / "gate-test" / "tasks"
            self.assertTrue(any(artifacts.rglob("review-01.gate.json")))
            merged = json.loads(next(artifacts.rglob("review-01.json")).read_text(encoding="utf-8"))
            self.assertEqual(merged["verdict"], "revision_required")

    def test_review_gate_is_skipped_when_reviewer_already_blocks(self):
        record = sample_record()
        spec = sample_spec()
        blocked_review = {
            "verdict": "revision_required", "summary": "需要修改",
            "blocking": [{
                "id": "R001", "category": "determinism", "location": "T01",
                "evidence": "状态规则缺少一个必要分支", "reason": "无法稳定复算",
                "required_change": "补齐分支",
            }],
            "verified": ["来源可用"], "suggestions": ["措辞可更简洁"],
        }
        passed_review = {
            "verdict": "passed", "summary": "修改后可用", "blocking": [],
            "verified": ["已落实"], "suggestions": [],
        }
        responses = [
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(blocked_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(passed_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            task_dir = root / "dataset" / "维度一" / "题目一"
            project.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (task_dir / "问题描述.txt").write_text("提交一份结果", encoding="utf-8")
            run = RubricRun(project, root / "dataset", RunOptions(concurrency=1, retries=0), run_id="skip-gate-test")
            run.client = FakeClient(responses)
            outcomes = run.execute(discover_tasks(root / "dataset"))
            self.assertEqual(outcomes[0].status, "COMPLETED")
            artifacts = project / ".rubric-generator" / "runs" / "skip-gate-test" / "tasks"
            self.assertFalse(any(artifacts.rglob("review-01.gate.json")))

    def test_failed_late_stage_resumes_without_repeating_completed_agents(self):
        record = sample_record()
        spec = sample_spec()
        blocked_review = {
            "verdict": "revision_required", "summary": "仍有分歧",
            "blocking": [{
                "id": "R001", "category": "determinism", "location": "T01",
                "evidence": "规则存在歧义", "reason": "可能改变分数",
                "required_change": "按裁决锁定",
            }],
            "verified": ["其他部分可用"], "suggestions": [],
        }
        arbitration = {
            "summary": "驳回该阻断并保持当前合同",
            "decisions": [{
                "issue_id": "R001", "decision": "reject",
                "basis": "现有规则已经确定", "binding_change": "不修改",
            }],
            "final_instructions": ["保持当前 record 与 spec"],
        }
        initial_responses = [
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(blocked_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(blocked_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_ARBITRATION_JSON\n" + json.dumps(arbitration, ensure_ascii=False) + "\nEND_ARBITRATION_JSON",
        ]
        final_patch = {"record_patch": [], "spec_patch": []}
        passed_review = {
            "verdict": "passed", "summary": "裁决已落实", "blocking": [],
            "verified": ["最终合同可用"], "suggestions": [],
        }
        resume_responses = [
            "BEGIN_FINAL_PATCH_JSON\n" + json.dumps(final_patch) + "\nEND_FINAL_PATCH_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(passed_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            dataset = root / "dataset"
            task_dir = dataset / "维度一" / "题目一"
            project.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (task_dir / "问题描述.txt").write_text("提交一份结果", encoding="utf-8")

            first = RubricRun(project, dataset, RunOptions(concurrency=1, retries=0), run_id="resume-test")
            first.client = FakeClient(initial_responses)
            self.assertEqual(first.execute(discover_tasks(dataset))[0].status, "FAILED")

            resumed = RubricRun(
                project, dataset,
                RunOptions(concurrency=1, retries=0, force=True, resume=True),
                run_id="resume-test",
            )
            resumed.client = FakeClient(resume_responses)
            outcome = resumed.execute(discover_tasks(dataset))[0]
            self.assertEqual(outcome.status, "AUTO_FINALIZED")
            self.assertTrue((task_dir / "rubric.md").is_file())
            task_status = json.loads(next((resumed.run_dir / "tasks").glob("*/status.json")).read_text(encoding="utf-8"))
            self.assertEqual(task_status["resume_count"], 1)
            self.assertNotIn("error", task_status)

    def test_final_compliance_can_apply_two_bounded_corrections(self):
        record = sample_record()
        spec = sample_spec()
        blocked_review = {
            "verdict": "revision_required", "summary": "仍有分歧",
            "blocking": [{
                "id": "R001", "category": "determinism", "location": "T01",
                "evidence": "规则存在歧义", "reason": "可能改变分数",
                "required_change": "按裁决锁定",
            }],
            "verified": [], "suggestions": [],
        }
        arbitration = {
            "summary": "保持当前合同并继续最终核验",
            "decisions": [{
                "issue_id": "R001", "decision": "reject",
                "basis": "现有规则已经确定", "binding_change": "不修改",
            }],
            "final_instructions": ["保持当前 record 与 spec"],
        }
        passed_with_suggestion = {
            "verdict": "passed", "summary": "主体可用",
            "blocking": [], "verified": ["主体规则可计算"],
            "suggestions": ["仍有一个可改变分数的边界歧义"],
        }
        gate = {
            "verdict": "promotion_required", "summary": "需提升",
            "promoted": [{
                "suggestion_index": 1, "id": "G001", "category": "determinism",
                "location": "T01", "evidence": "存在可复现反例",
                "reason": "不同评卷人会得到不同状态", "required_change": "写死互斥边界",
            }],
        }
        passed_review = {
            "verdict": "passed", "summary": "最终合同可用",
            "blocking": [], "verified": ["歧义已消除"], "suggestions": [],
        }
        no_op_replace = {
            "record_patch": [],
            "spec_patch": [{
                "op": "replace", "path": "/completion/atoms/0/state_rule",
                "value": spec["completion"]["atoms"][0]["state_rule"],
            }],
        }
        empty_patch = {"record_patch": [], "spec_patch": []}
        responses = [
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(blocked_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_AUTHORING_RECORD_JSON\n" + json.dumps(record, ensure_ascii=False) + "\nEND_AUTHORING_RECORD_JSON",
            "BEGIN_RUBRIC_SPEC_JSON\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_RUBRIC_SPEC_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(blocked_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_ARBITRATION_JSON\n" + json.dumps(arbitration, ensure_ascii=False) + "\nEND_ARBITRATION_JSON",
            "BEGIN_FINAL_PATCH_JSON\n" + json.dumps(empty_patch) + "\nEND_FINAL_PATCH_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(passed_with_suggestion, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_REVIEW_GATE_JSON\n" + json.dumps(gate, ensure_ascii=False) + "\nEND_REVIEW_GATE_JSON",
            "BEGIN_FINAL_PATCH_JSON\n" + json.dumps(no_op_replace) + "\nEND_FINAL_PATCH_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(passed_with_suggestion, ensure_ascii=False) + "\nEND_REVIEW_JSON",
            "BEGIN_REVIEW_GATE_JSON\n" + json.dumps(gate, ensure_ascii=False) + "\nEND_REVIEW_GATE_JSON",
            "BEGIN_FINAL_PATCH_JSON\n" + json.dumps(no_op_replace) + "\nEND_FINAL_PATCH_JSON",
            "BEGIN_REVIEW_JSON\n" + json.dumps(passed_review, ensure_ascii=False) + "\nEND_REVIEW_JSON",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            task_dir = root / "dataset" / "维度一" / "题目一"
            project.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (task_dir / "问题描述.txt").write_text("提交一份结果", encoding="utf-8")
            run = RubricRun(project, root / "dataset", RunOptions(concurrency=1, retries=0), run_id="two-fixes")
            run.client = FakeClient(responses)
            outcome = run.execute(discover_tasks(root / "dataset"))[0]
            self.assertEqual(outcome.status, "AUTO_FINALIZED")
            artifacts = next((run.run_dir / "tasks").glob("*/artifacts"))
            self.assertTrue(any(artifacts.glob("author-final-correction-patch-02-*.validated.json")))
            self.assertTrue(any(artifacts.glob("final-review-03-*.validated.json")))


if __name__ == "__main__":
    unittest.main()
