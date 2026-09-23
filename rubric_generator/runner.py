from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import prompts
from .audit import (
    apply_review_gate,
    audit_arbitration_payload,
    audit_authoring_record,
    audit_file,
    audit_review_gate_payload,
    audit_review_payload,
    normalize_authoring_record,
)
from .config import load_project_env, load_project_settings
from .dataset import DatasetTask, copy_task_snapshot, discover_tasks, is_reference_rubric
from .materials import prepare_material_evidence
from .opencode_client import OpenCodeClient, OpenCodeError, extract_json
from .rubric_spec import (
    audit_authoring_record_spec_alignment,
    audit_rubric_spec,
    audit_rendered_rubric,
    normalize_rubric_spec,
    render_rubric,
    semantic_fingerprint,
)


FINAL_STATES = {
    "COMPLETED", "AUTO_FINALIZED", "SEMANTIC_AUDIT_FAILED", "AUDIT_FAILED",
    "INPUT_BLOCKED", "SKIPPED", "FAILED",
}


@dataclass
class RunOptions:
    concurrency: int = 3
    max_revision_rounds: int = 1
    # Keep the main reviewer-directed rewrite at one round, but allow a second
    # narrowly scoped final correction when the first correction itself leaves
    # a score-changing ambiguity.  The loop remains strictly bounded.
    max_final_compliance_repairs: int = 2
    max_format_retries: int = 1
    max_schema_retries: int = 1
    force: bool = False
    timeout_seconds: int = 900
    retries: int = 1
    resume: bool = False


@dataclass
class TaskOutcome:
    task_id: str
    relative_dir: str
    status: str
    rubric_file: str | None = None
    revision_rounds: int = 0
    audit_issues: list[str] | None = None
    error: str | None = None


class RubricRun:
    def __init__(self, project_dir: Path, dataset_root: Path, options: RunOptions, run_id: str | None = None):
        self.project_dir = project_dir.resolve()
        self.dataset_root = dataset_root.resolve()
        self.options = options
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = self.project_dir / ".rubric-generator" / "runs" / self.run_id
        if _is_within(self.run_dir, self.dataset_root):
            raise ValueError(
                "运行留痕目录位于所选数据集内部；请传入独立的数据集目录，"
                "不要把项目根目录或其上级目录作为 --dataset"
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._log_lock = threading.Lock()
        load_project_env(self.project_dir)
        settings = load_project_settings(self.project_dir)
        self.client = OpenCodeClient(
            self.project_dir,
            options.timeout_seconds,
            options.retries,
            logger=self._log,
            model_by_agent=settings.agent_models,
        )

    def execute(self, tasks: list[DatasetTask] | None = None) -> list[TaskOutcome]:
        selected = tasks if tasks is not None else discover_tasks(self.dataset_root)
        if not selected:
            raise RuntimeError(f"未找到问题描述.txt: {self.dataset_root}")
        tool_probe = getattr(self.client, "check_tool_roundtrip", None)
        if callable(tool_probe):
            self._log("正在执行模型工具续写兼容性检查；未通过时不会启动题目任务")
            try:
                tool_probe()
            except OpenCodeError as exc:
                raise RuntimeError(
                    "模型网关未通过工具续写兼容性检查，已停止本次运行；"
                    "题目检查点和数据集均未改变。请稍后重试或更换支持工具调用的模型接口。"
                ) from exc
            self._log("模型工具续写兼容性检查通过")
        total = len(selected)
        self._log(
            f"运行 {self.run_id} 开始：共 {total} 道题，并发数 {self.options.concurrency}；"
            f"留痕目录 {self.run_dir}"
        )
        outcomes: list[TaskOutcome] = []
        with ThreadPoolExecutor(max_workers=max(1, min(self.options.concurrency, 3))) as pool:
            future_map = {pool.submit(self._process_task, task): task for task in selected}
            for future in as_completed(future_map):
                task = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # one failed task must not stop the queue
                    result = TaskOutcome(task.task_id, str(task.relative_dir), "FAILED", error=str(exc))
                outcomes.append(result)
                self._write_summary(outcomes)
                self._log(f"总体进度 {len(outcomes)}/{total}：{task.relative_dir} -> {result.status}")
        outcomes.sort(key=lambda item: item.relative_dir.casefold())
        self._write_summary(outcomes)
        self._log(f"运行 {self.run_id} 结束：已处理 {total} 道题")
        return outcomes

    def _process_task(self, task: DatasetTask) -> TaskOutcome:
        if task.rubric_file.exists() and not self.options.force:
            self._log(f"[{task.task_id}] 已有 rubric.md，跳过")
            return TaskOutcome(task.task_id, str(task.relative_dir), "SKIPPED", str(task.rubric_file))

        self._log(f"[{task.task_id}] 开始：{task.relative_dir}")
        task_run = self.run_dir / "tasks" / task.task_id
        input_root = task_run / "input"
        artifacts = task_run / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        status_file = task_run / "status.json"
        state: dict[str, object] = {}
        if self.options.resume and status_file.is_file():
            try:
                previous_state = json.loads(status_file.read_text(encoding="utf-8"))
                if isinstance(previous_state, dict):
                    state.update(previous_state)
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        state.update({
            "task_id": task.task_id,
            "relative_dir": str(task.relative_dir),
            "status": "RESUMING" if self.options.resume else "AUTHORING",
        })
        state.setdefault("revision_rounds", 0)
        state.setdefault("started_at", _now())
        if self.options.resume:
            state["resumed_at"] = _now()
            state["resume_count"] = int(state.get("resume_count", 0)) + 1
        self._write_json(status_file, state)
        revision_rounds = 0

        try:
            existing_snapshot = input_root / task.relative_dir
            if self.options.resume and existing_snapshot.is_dir():
                source_fingerprint = _input_fingerprint(task.task_dir, exclude_rubrics=True)
                snapshot_fingerprint = _input_fingerprint(existing_snapshot, exclude_rubrics=False)
                if source_fingerprint != snapshot_fingerprint:
                    raise OSError(
                        "题目或源素材自上次运行后已变化，不能复用旧检查点；"
                        "请使用 generate 启动新运行"
                    )
            snapshot_dir = copy_task_snapshot(task, input_root)
            self._write_json(task_run / "manifest.json", {
                "task_id": task.task_id,
                "source_dataset": str(task.dataset_root),
                "source_task": str(task.task_dir),
                "snapshot_task": str(snapshot_dir),
                "files": [
                    str(path.relative_to(snapshot_dir))
                    for path in snapshot_dir.rglob("*") if path.is_file()
                ],
                "input_fingerprint": _input_fingerprint(snapshot_dir, exclude_rubrics=False),
            })
            material_manifest = prepare_material_evidence(snapshot_dir, task_run / "materials")
            scratch_dir = task_run / "scratch"
            scratch_dir.mkdir(parents=True, exist_ok=True)
            if self.options.resume:
                self._bootstrap_legacy_checkpoints(artifacts)

            state["status"] = "ANALYZING"
            self._write_json(task_run / "status.json", state)
            self._log(f"[{task.task_id}] 正在审题求解并设计六项评分合同")
            authoring_record = self._call_authoring_record(
                task.task_id, artifacts, "author-analysis",
                prompts.author_analysis(self.project_dir, snapshot_dir, material_manifest, scratch_dir),
                f"analyze {task.task_id}",
            )
            current_record = artifacts / "authoring-record-00.json"
            self._write_json(current_record, authoring_record)
            record_history = [current_record]

            self._log(f"[{task.task_id}] 正在生成结构化 Rubric 规格并确定性渲染")
            rubric_spec = self._call_rubric_spec(
                task.task_id, artifacts, "author-spec-00",
                prompts.author_spec(self.project_dir, snapshot_dir, material_manifest, current_record),
                f"author spec {task.task_id} initial", snapshot_dir, authoring_record,
            )
            current_spec = artifacts / "rubric-spec-00.json"
            self._write_json(current_spec, rubric_spec)
            current_rubric = artifacts / "rubric-00.md"
            self._write_text(current_rubric, render_rubric(rubric_spec))
            spec_history = [current_spec]

            review_files: list[Path] = []
            previous_review: Path | None = None
            passed = False
            final_review: dict[str, object] | None = None
            for review_index in range(1, self.options.max_revision_rounds + 2):
                state["status"] = "REVIEWING"
                self._write_json(task_run / "status.json", state)
                self._log(f"[{task.task_id}] 正在进行第 {review_index} 次独立审核")
                raw_review_data = self._call_validated_json_agent(
                    task.task_id, artifacts, f"review-{review_index:02d}", "rubric-reviewer",
                    prompts.review(
                        self.project_dir, snapshot_dir, material_manifest, current_record,
                        current_spec, current_rubric, review_index, previous_review=previous_review,
                    ),
                    f"review {task.task_id} {review_index}",
                    "BEGIN_REVIEW_JSON", "END_REVIEW_JSON",
                    audit_review_payload, "reviewer output",
                )
                raw_review_path = artifacts / f"review-{review_index:02d}.raw.json"
                self._write_json(raw_review_path, raw_review_data)
                raw_blocking = raw_review_data.get("blocking")
                if raw_review_data.get("verdict") == "passed" and not raw_blocking:
                    review_data = self._apply_review_classification_gate(
                        task.task_id, artifacts, f"review-{review_index:02d}", raw_review_data,
                        snapshot_dir, current_record, current_spec, current_rubric, raw_review_path,
                    )
                else:
                    review_data = raw_review_data
                review_path = artifacts / f"review-{review_index:02d}.json"
                self._write_json(review_path, review_data)
                review_files.append(review_path)
                previous_review = review_path
                final_review = review_data
                blocking = review_data.get("blocking")
                blocking_items = blocking if isinstance(blocking, list) else []
                if review_data.get("verdict") == "passed" and not blocking_items:
                    passed = True
                    break
                if revision_rounds >= self.options.max_revision_rounds:
                    break

                revision_rounds += 1
                state.update({"status": "REVISING", "revision_rounds": revision_rounds})
                self._write_json(task_run / "status.json", state)
                self._log(f"[{task.task_id}] 审核未通过，正在进行第 {revision_rounds} 轮修改")
                authoring_record = self._call_authoring_record(
                    task.task_id, artifacts, f"author-revision-record-{revision_rounds:02d}",
                    prompts.author_record_revision(
                        self.project_dir, snapshot_dir, material_manifest, current_record,
                        current_spec, review_path, revision_rounds,
                    ),
                    f"revise record {task.task_id} {revision_rounds}",
                )
                current_record = artifacts / f"authoring-record-{revision_rounds:02d}.json"
                self._write_json(current_record, authoring_record)
                record_history.append(current_record)
                rubric_spec = self._call_rubric_spec(
                    task.task_id, artifacts, f"author-spec-{revision_rounds:02d}",
                    prompts.author_spec_revision(
                        self.project_dir, snapshot_dir, material_manifest, current_record,
                        current_spec, review_path, revision_rounds,
                    ),
                    f"revise spec {task.task_id} {revision_rounds}", snapshot_dir, authoring_record,
                )
                current_spec = artifacts / f"rubric-spec-{revision_rounds:02d}.json"
                self._write_json(current_spec, rubric_spec)
                spec_history.append(current_spec)
                current_rubric = artifacts / f"rubric-{revision_rounds:02d}.md"
                self._write_text(current_rubric, render_rubric(rubric_spec))

            auto_finalized = False
            arbitration_path: Path | None = None
            if not passed:
                state["status"] = "ARBITRATING"
                self._write_json(task_run / "status.json", state)
                self._log(f"[{task.task_id}] 一轮修改后仍有阻断项，进入自动裁决")
                arbitration = self._call_validated_json_agent(
                    task.task_id, artifacts, "arbitration", "rubric-arbitrator",
                    prompts.arbitrate(
                        self.project_dir, snapshot_dir, material_manifest, current_record,
                        current_spec, record_history, spec_history, review_files,
                    ),
                    f"arbitrate {task.task_id}",
                    "BEGIN_ARBITRATION_JSON", "END_ARBITRATION_JSON",
                    audit_arbitration_payload, "arbitration output",
                )
                arbitration_path = artifacts / "arbitration.json"
                self._write_json(arbitration_path, arbitration)

                state["status"] = "FINALIZING"
                self._write_json(task_run / "status.json", state)
                final_patch = self._call_final_patch(
                    task.task_id, artifacts, "author-final-patch",
                    prompts.author_final_patch(
                        self.project_dir, snapshot_dir, material_manifest, current_record,
                        current_spec, arbitration_path,
                    ),
                    f"finalize patch {task.task_id}", snapshot_dir, current_record, current_spec,
                    require_change=_arbitration_requires_change(arbitration),
                )
                authoring_record, rubric_spec = self._apply_final_patch_files(
                    current_record, current_spec, final_patch,
                )
                self._write_json(artifacts / "author-final-patch.json", final_patch)
                current_record = artifacts / "authoring-record-finalized.json"
                self._write_json(current_record, authoring_record)
                record_history.append(current_record)
                current_spec = artifacts / "rubric-spec-finalized.json"
                self._write_json(current_spec, rubric_spec)
                spec_history.append(current_spec)
                current_rubric = artifacts / "rubric-finalized.md"
                self._write_text(current_rubric, render_rubric(rubric_spec))
                auto_finalized = True

                compliance_passed = False
                for compliance_index in range(self.options.max_final_compliance_repairs + 1):
                    state["status"] = "FINAL_REVIEWING"
                    self._write_json(task_run / "status.json", state)
                    final_review_phase = f"final-review-{compliance_index + 1:02d}"
                    if compliance_index:
                        # A later compliance review must be bound to the corrected
                        # candidate.  Otherwise resume could reuse a valid JSON
                        # checkpoint that reviewed an older rubric version.
                        candidate_hash = semantic_fingerprint(authoring_record, rubric_spec)["sha256"][:12]
                        final_review_phase = f"{final_review_phase}-{candidate_hash}"
                    raw_compliance_data = self._call_validated_json_agent(
                        task.task_id, artifacts, final_review_phase,
                        "rubric-reviewer",
                        prompts.review(
                            self.project_dir, snapshot_dir, material_manifest, current_record,
                            current_spec, current_rubric, 100 + compliance_index,
                            previous_review=previous_review, arbitration_file=arbitration_path,
                        ),
                        f"verify final {task.task_id} {compliance_index}",
                        "BEGIN_REVIEW_JSON", "END_REVIEW_JSON",
                        audit_review_payload, "final reviewer output",
                    )
                    raw_compliance_path = artifacts / f"{final_review_phase}.raw.json"
                    self._write_json(raw_compliance_path, raw_compliance_data)
                    raw_blocking = raw_compliance_data.get("blocking")
                    if raw_compliance_data.get("verdict") == "passed" and not raw_blocking:
                        compliance_data = self._apply_review_classification_gate(
                            task.task_id, artifacts, final_review_phase,
                            raw_compliance_data, snapshot_dir, current_record, current_spec,
                            current_rubric, raw_compliance_path,
                        )
                    else:
                        compliance_data = raw_compliance_data
                    compliance_path = artifacts / f"{final_review_phase}.json"
                    self._write_json(compliance_path, compliance_data)
                    final_review = compliance_data
                    blocking = compliance_data.get("blocking")
                    blocking_items = blocking if isinstance(blocking, list) else []
                    if compliance_data.get("verdict") == "passed" and not blocking_items:
                        compliance_passed = True
                        break
                    if compliance_index >= self.options.max_final_compliance_repairs:
                        break

                    state["status"] = "FINAL_CORRECTING"
                    self._write_json(task_run / "status.json", state)
                    correction_phase = "author-final-correction-patch"
                    if compliance_index:
                        candidate_hash = semantic_fingerprint(authoring_record, rubric_spec)["sha256"][:12]
                        correction_phase = (
                            f"author-final-correction-patch-{compliance_index + 1:02d}-{candidate_hash}"
                        )
                    final_patch = self._call_final_patch(
                        task.task_id, artifacts, correction_phase,
                        prompts.author_final_patch(
                            self.project_dir, snapshot_dir, material_manifest, current_record,
                            current_spec, arbitration_path, compliance_review=compliance_path,
                        ),
                        f"correct final patch {task.task_id}", snapshot_dir, current_record, current_spec,
                        require_change=True,
                    )
                    authoring_record, rubric_spec = self._apply_final_patch_files(
                        current_record, current_spec, final_patch,
                    )
                    self._write_json(artifacts / f"{correction_phase}.json", final_patch)
                    self._write_json(artifacts / "author-final-correction-patch.json", final_patch)
                    current_record = artifacts / "authoring-record-final-corrected.json"
                    self._write_json(current_record, authoring_record)
                    current_spec = artifacts / "rubric-spec-final-corrected.json"
                    self._write_json(current_spec, rubric_spec)
                    current_rubric = artifacts / "rubric-final-corrected.md"
                    self._write_text(current_rubric, render_rubric(rubric_spec))

                if not compliance_passed:
                    state.update({"status": "SEMANTIC_AUDIT_FAILED", "completed_at": _now(), "final_review": final_review})
                    self._write_json(task_run / "status.json", state)
                    return TaskOutcome(
                        task.task_id, str(task.relative_dir), "SEMANTIC_AUDIT_FAILED",
                        revision_rounds=revision_rounds,
                        error="Final binding-compliance review failed",
                    )

            state["status"] = "AUDITING"
            self._write_json(task_run / "status.json", state)
            self._log(f"[{task.task_id}] 正在执行结构、自包含与可计算性审计")
            audit = audit_file(current_rubric)
            alignment_issues = audit_authoring_record_spec_alignment(authoring_record, rubric_spec)
            final_issues = [*audit.issues, *alignment_issues]
            if final_issues:
                state.update({"status": "AUDIT_FAILED", "completed_at": _now(), "audit_issues": final_issues})
                self._write_json(task_run / "status.json", state)
                return TaskOutcome(
                    task.task_id, str(task.relative_dir), "AUDIT_FAILED", None,
                    revision_rounds, final_issues,
                )

            output = self._commit_rubric(task, current_rubric)
            fingerprint = semantic_fingerprint(authoring_record, rubric_spec)
            self._write_json(artifacts / "semantic-fingerprint.json", fingerprint)
            final_status = "AUTO_FINALIZED" if auto_finalized else "COMPLETED"
            # A resumed task may carry diagnostic fields from its previous
            # failed attempt.  Once delivery succeeds, status.json must describe
            # the current terminal state rather than retain a stale failure.
            state.pop("error", None)
            state.pop("audit_issues", None)
            state.update({
                "status": final_status,
                "completed_at": _now(),
                "output": str(output),
                "final_review": final_review,
                "authoring_record": str(current_record),
                "rubric_spec": str(current_spec),
                "semantic_fingerprint": fingerprint["sha256"],
                "audit_issues": [],
                "audit_advisories": list(audit.advisories),
            })
            self._write_json(task_run / "status.json", state)
            self._log(f"[{task.task_id}] 完成：{final_status}；输出 {output}")
            return TaskOutcome(task.task_id, str(task.relative_dir), final_status, str(output), revision_rounds, [])
        except Exception as exc:
            status = "INPUT_BLOCKED" if isinstance(exc, (OSError, UnicodeError)) else "FAILED"
            state.update({"status": status, "completed_at": _now(), "error": str(exc)})
            self._write_json(task_run / "status.json", state)
            self._log(f"[{task.task_id}] 失败：{status}；{exc}")
            return TaskOutcome(
                task.task_id, str(task.relative_dir), status,
                revision_rounds=revision_rounds, error=str(exc),
            )

    def _apply_review_classification_gate(
        self,
        task_id: str,
        artifacts: Path,
        phase: str,
        review_data: dict[str, object],
        task_dir: Path,
        authoring_record: Path,
        spec_file: Path,
        rubric_file: Path,
        raw_review_file: Path,
    ) -> dict[str, object]:
        suggestions = review_data.get("suggestions")
        suggestion_items = suggestions if isinstance(suggestions, list) else []
        if not suggestion_items:
            return review_data
        self._log(f"[{task_id}] Reviewer 给出 {len(suggestion_items)} 条建议，正在执行严重性分类门禁")
        gate = self._call_validated_json_agent(
            task_id, artifacts, phase + "-gate", "rubric-review-classifier",
            prompts.classify_review(
                self.project_dir, task_dir, authoring_record, spec_file, rubric_file,
                raw_review_file,
            ),
            f"classify review suggestions {task_id}",
            "BEGIN_REVIEW_GATE_JSON", "END_REVIEW_GATE_JSON",
            lambda payload: audit_review_gate_payload(payload, len(suggestion_items)),
            "review classification gate",
        )
        self._write_json(artifacts / f"{phase}.gate.json", gate)
        merged = apply_review_gate(review_data, gate)
        merged_issues = audit_review_payload(merged)
        if merged_issues:
            raise OpenCodeError("Invalid gated review: " + "; ".join(merged_issues))
        promoted = gate.get("promoted")
        promoted_count = len(promoted) if isinstance(promoted, list) else 0
        if promoted_count:
            self._log(f"[{task_id}] 分类门禁将 {promoted_count} 条建议提升为 blocking")
        return merged

    def _call_authoring_record(self, task_id: str, artifacts: Path, phase: str, prompt: str, title: str) -> dict[str, object]:
        checkpoint = artifacts / f"{phase}.validated.json"
        cached = self._read_checkpoint(checkpoint, audit_authoring_record)
        if cached is not None:
            self._log(f"[{task_id}] {phase} 使用已校验检查点")
            return cached
        issues: list[str] = []
        for attempt in range(self.options.max_schema_retries + 1):
            suffix = "" if attempt == 0 else f".schema-retry-{attempt:02d}"
            attempt_prompt = prompt
            if attempt:
                invalid = artifacts / f"{phase}.schema-invalid-{attempt - 1:02d}.json"
                attempt_prompt += self._schema_correction_instruction(invalid, issues, "authoring record")
                self._log(f"[{task_id}] {phase} 结构校验未通过，进行第 {attempt} 次纠错")
            raw = self._call_json_agent(
                task_id, artifacts, phase + suffix, "rubric-author", attempt_prompt,
                title + suffix, "BEGIN_AUTHORING_RECORD_JSON", "END_AUTHORING_RECORD_JSON",
            )
            normalized, changes = normalize_authoring_record(raw)
            if changes:
                self._write_json(artifacts / f"{phase + suffix}.normalizations.json", {"changes": changes})
            issues = audit_authoring_record(normalized)
            if not issues:
                self._write_json(checkpoint, normalized)
                return normalized
            self._write_json(artifacts / f"{phase}.schema-invalid-{attempt:02d}.json", normalized)
        raise OpenCodeError("Invalid authoring record after bounded correction: " + "; ".join(issues))

    def _call_rubric_spec(
        self,
        task_id: str,
        artifacts: Path,
        phase: str,
        prompt: str,
        title: str,
        task_dir: Path,
        record: dict[str, object],
    ) -> dict[str, object]:
        normalization_changes: list[dict[str, str]] = []

        def validate(spec: dict[str, object]) -> list[str]:
            normalization_changes.extend(normalize_rubric_spec(spec))
            rendered_issues, _ = audit_rendered_rubric(render_rubric(spec))
            return [
                *audit_rubric_spec(spec, task_dir),
                *audit_authoring_record_spec_alignment(record, spec),
                *rendered_issues,
            ]

        result = self._call_validated_json_agent(
            task_id, artifacts, phase, "rubric-author", prompt, title,
            "BEGIN_RUBRIC_SPEC_JSON", "END_RUBRIC_SPEC_JSON", validate, "rubric spec",
        )
        if normalization_changes:
            self._write_json(
                artifacts / f"{phase}.normalizations.json",
                {"changes": normalization_changes},
            )
        return result

    def _call_final_patch(
        self,
        task_id: str,
        artifacts: Path,
        phase: str,
        prompt: str,
        title: str,
        task_dir: Path,
        record_file: Path,
        spec_file: Path,
        *,
        require_change: bool = False,
    ) -> dict[str, object]:
        def validate(payload: dict[str, object]) -> list[str]:
            issues = audit_final_patch_payload(payload)
            if issues:
                return issues
            if require_change and not payload.get("record_patch") and not payload.get("spec_patch"):
                return ["binding arbitration or blocking compliance review requires a non-empty patch"]
            try:
                record, spec = self._apply_final_patch_files(record_file, spec_file, payload)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                return [f"patch application failed: {exc}"]
            return [
                *audit_authoring_record(record),
                *audit_rubric_spec(spec, task_dir),
                *audit_authoring_record_spec_alignment(record, spec),
                *audit_rendered_rubric(render_rubric(spec))[0],
            ]

        return self._call_validated_json_agent(
            task_id, artifacts, phase, "rubric-finalizer", prompt, title,
            "BEGIN_FINAL_PATCH_JSON", "END_FINAL_PATCH_JSON", validate, "final patch",
        )

    def _call_validated_json_agent(
        self,
        task_id: str,
        artifacts: Path,
        phase: str,
        agent: str,
        prompt: str,
        title: str,
        begin: str,
        end: str,
        validator: Callable[[dict[str, object]], list[str]],
        payload_name: str,
    ) -> dict[str, object]:
        checkpoint = artifacts / f"{phase}.validated.json"
        cached = self._read_checkpoint(checkpoint, validator)
        if cached is not None:
            self._log(f"[{task_id}] {phase} 使用已校验检查点")
            return cached
        issues: list[str] = []
        for attempt in range(self.options.max_schema_retries + 1):
            suffix = "" if attempt == 0 else f".schema-retry-{attempt:02d}"
            attempt_prompt = prompt
            if attempt:
                invalid = artifacts / f"{phase}.schema-invalid-{attempt - 1:02d}.json"
                attempt_prompt += self._schema_correction_instruction(
                    invalid, issues, payload_name,
                    inline_invalid=agent == "rubric-finalizer",
                )
                self._log(f"[{task_id}] {phase} 结构校验未通过，进行第 {attempt} 次纠错")
            payload = self._call_json_agent(
                task_id, artifacts, phase + suffix, agent, attempt_prompt, title + suffix,
                begin, end,
            )
            issues = validator(payload)
            if not issues:
                self._write_json(checkpoint, payload)
                return payload
            self._write_json(artifacts / f"{phase}.schema-invalid-{attempt:02d}.json", payload)
        raise OpenCodeError(f"Invalid {payload_name} after bounded correction: " + "; ".join(issues))

    def _read_checkpoint(
        self,
        path: Path,
        validator: Callable[[dict[str, object]], list[str]],
    ) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or validator(payload):
            return None
        return payload

    def _bootstrap_legacy_checkpoints(self, artifacts: Path) -> None:
        """Make artifacts from pre-checkpoint runs resumable without model calls."""
        mappings: list[tuple[str, str]] = [
            ("author-analysis", "authoring-record-00.json"),
            ("author-spec-00", "rubric-spec-00.json"),
            ("arbitration", "arbitration.json"),
        ]
        for record in artifacts.glob("authoring-record-[0-9][0-9].json"):
            if record.name != "authoring-record-00.json":
                index = record.stem.rsplit("-", 1)[-1]
                mappings.append((f"author-revision-record-{index}", record.name))
        for spec in artifacts.glob("rubric-spec-[0-9][0-9].json"):
            if spec.name != "rubric-spec-00.json":
                index = spec.stem.rsplit("-", 1)[-1]
                mappings.append((f"author-spec-{index}", spec.name))
        for raw_review in artifacts.glob("review-[0-9][0-9].raw.json"):
            phase = raw_review.name.removesuffix(".raw.json")
            mappings.append((phase, raw_review.name))
            gate = artifacts / f"{phase}.gate.json"
            if gate.is_file():
                mappings.append((f"{phase}-gate", gate.name))
        for phase, source_name in mappings:
            source = artifacts / source_name
            target = artifacts / f"{phase}.validated.json"
            if source.is_file() and not target.exists():
                try:
                    payload = json.loads(source.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    self._write_json(target, payload)

    @staticmethod
    def _apply_final_patch_files(
        record_file: Path,
        spec_file: Path,
        payload: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        record = json.loads(record_file.read_text(encoding="utf-8"))
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        return (
            apply_json_patch(record, payload.get("record_patch", [])),
            apply_json_patch(spec, payload.get("spec_patch", [])),
        )

    def _call_json_agent(
        self,
        task_id: str,
        artifacts: Path,
        phase: str,
        agent: str,
        prompt: str,
        title: str,
        begin: str,
        end: str,
    ) -> dict[str, object]:
        last_error: OpenCodeError | None = None
        if self.options.resume:
            for attempt in range(self.options.max_format_retries + 1):
                suffix = "" if attempt == 0 else f".format-retry-{attempt:02d}"
                response_file = artifacts / f"{phase + suffix}.response.txt"
                if not response_file.is_file():
                    continue
                try:
                    cached_text = response_file.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                try:
                    cached = extract_json(cached_text, begin, end)
                except OpenCodeError:
                    if begin not in cached_text or end not in cached_text:
                        continue
                    try:
                        cached = self._repair_complete_json_response(
                            task_id, artifacts, phase + suffix, cached_text, begin, end,
                        )
                    except OpenCodeError as exc:
                        self._log(f"[{task_id}] {phase + suffix} 已有完整标记响应，但 JSON 修复失败：{exc}")
                        continue
                    self._log(f"[{task_id}] {phase + suffix} 已从完整标记响应修复局部 JSON 损坏")
                self._log(f"[{task_id}] {phase + suffix} 复用已完整返回的模型响应")
                return cached
        for attempt in range(self.options.max_format_retries + 1):
            suffix = "" if attempt == 0 else f".format-retry-{attempt:02d}"
            attempt_prompt = prompt
            if attempt:
                attempt_prompt += (
                    "\n\n上一次响应无法解析。只输出要求标记包围的完整、严格合法 JSON；"
                    "字符串双引号必须转义，不得有代码围栏、未转义控制字符或标记外说明。"
                )
                self._log(f"[{task_id}] {phase} 输出格式异常，进行第 {attempt} 次格式重试")
            response = self.client.run_agent(agent, attempt_prompt, title + suffix)
            stem = phase + suffix
            self._write_text(artifacts / f"{stem}.events.jsonl", response.raw_output)
            self._write_text(artifacts / f"{stem}.response.txt", response.text + "\n")
            try:
                return extract_json(response.text, begin, end)
            except OpenCodeError as exc:
                if begin in response.text and end in response.text:
                    try:
                        return self._repair_complete_json_response(
                            task_id, artifacts, stem, response.text, begin, end,
                        )
                    except OpenCodeError as repair_exc:
                        last_error = repair_exc
                if response.terminal_error:
                    if last_error is None:
                        last_error = OpenCodeError(
                            f"{phase} provider failed before complete JSON: {response.terminal_error}"
                        )
                    break
                if last_error is None:
                    last_error = exc
        raise last_error or OpenCodeError(f"{phase} returned invalid JSON")

    def _repair_complete_json_response(
        self,
        task_id: str,
        artifacts: Path,
        stem: str,
        damaged_text: str,
        begin: str,
        end: str,
    ) -> dict[str, object]:
        start = damaged_text.find(begin)
        stop = damaged_text.rfind(end)
        if start < 0 or stop < start:
            raise OpenCodeError("JSON repair requires a complete marker pair")
        marked = damaged_text[start: stop + len(end)]
        local_repair = self._try_local_stream_overlap_repair(marked, begin, end)
        if local_repair is not None:
            payload, repair_note = local_repair
            self._write_json(artifacts / f"{stem}.json-repair.local.json", repair_note)
            self._log(f"[{task_id}] {stem} 已确定性移除流恢复产生的重复截断行")
            return payload
        prompt = (
            "修复下列完整标记响应中的最小 JSON 语法损坏。"
            "不得重新研究、改写、补充或删减任何完整内容；保持原标记。\n\n"
            + marked
        )
        repair_stem = stem + ".json-repair"
        self._log(f"[{task_id}] {stem} 标记完整但 JSON 受损，正在执行局部语法修复")
        response = self.client.run_agent(
            "rubric-json-repair", prompt, f"repair JSON {task_id} {stem}",
        )
        self._write_text(artifacts / f"{repair_stem}.events.jsonl", response.raw_output)
        self._write_text(artifacts / f"{repair_stem}.response.txt", response.text + "\n")
        try:
            return extract_json(response.text, begin, end)
        except OpenCodeError as exc:
            if response.terminal_error:
                raise OpenCodeError(
                    f"{repair_stem} provider failed before valid repaired JSON: "
                    f"{response.terminal_error}"
                ) from exc
            raise OpenCodeError(f"{repair_stem} returned invalid repaired JSON: {exc}") from exc

    @staticmethod
    def _try_local_stream_overlap_repair(
        marked_text: str,
        begin: str,
        end: str,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        start = marked_text.find(begin) + len(begin)
        stop = marked_text.rfind(end)
        body = marked_text[start:stop].strip()
        try:
            json.loads(body)
            return None
        except json.JSONDecodeError as exc:
            error_index = exc.lineno - 1
        lines = body.splitlines()
        key_pattern = re.compile(r'^\s*"([^"\\]+)"\s*:')
        for index in (error_index, error_index - 1):
            if index < 0 or index + 1 >= len(lines):
                continue
            broken_match = key_pattern.match(lines[index])
            resumed_match = key_pattern.match(lines[index + 1])
            if not broken_match or not resumed_match:
                continue
            if broken_match.group(1) != resumed_match.group(1):
                continue
            # A streaming seam commonly leaves an unterminated first copy of
            # a field and then resumes by emitting that field again in full.
            # Only remove the first line when its quote count is odd and the
            # resulting entire document parses; otherwise defer to the
            # bounded, semantics-preserving repair agent.
            if lines[index].count('"') % 2 == 0:
                continue
            candidate_lines = lines[:index] + lines[index + 1:]
            candidate_text = "\n".join(candidate_lines)
            try:
                payload = json.loads(candidate_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            return payload, {
                "repair": "removed_truncated_duplicate_field_line",
                "json_line": index + 1,
                "field": broken_match.group(1),
                "removed_sha256": hashlib.sha256(lines[index].encode("utf-8")).hexdigest(),
            }
        return None

    def _schema_correction_instruction(
        self,
        invalid_path: Path,
        issues: list[str],
        payload_name: str,
        *,
        inline_invalid: bool = False,
    ) -> str:
        relative = invalid_path.resolve().relative_to(self.project_dir).as_posix()
        if inline_invalid:
            try:
                invalid_value = json.loads(invalid_path.read_text(encoding="utf-8"))
                invalid_input = (
                    "无效版本如下（不得读取路径）：\n"
                    + json.dumps(invalid_value, ensure_ascii=False, separators=(",", ":"))
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                invalid_input = "无效版本不可读；请仅依据原始输入和下列校验错误重新输出。"
        else:
            invalid_input = f"无效的 {payload_name} 已保存于：{relative}\n读取无效版本后修正。"
        return (
            "\n\n上一次输出可以解析，但未通过机器结构校验。"
            f"{invalid_input}\n"
            "只修复下列错误，不重新设计已经正确的事实、判断或评分契约：\n"
            + json.dumps(issues, ensure_ascii=False, indent=2)
            + "\n输出修正后的完整对象，继续使用原提示要求的标记。"
        )

    def _commit_rubric(self, task: DatasetTask, candidate: Path) -> Path:
        destination = task.rubric_file
        if destination.exists():
            if not self.options.force:
                raise FileExistsError(f"Rubric already exists: {destination}")
            backup = self.run_dir / "backups" / task.relative_dir / "rubric.md"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        text = candidate.read_text(encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".rubric-", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp_name, destination)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return destination

    def _write_summary(self, outcomes: list[TaskOutcome]) -> None:
        with self._write_lock:
            merged = {item.task_id: item for item in outcomes}
            summary_path = self.run_dir / "summary.json"
            if self.options.resume and summary_path.is_file():
                try:
                    previous = json.loads(summary_path.read_text(encoding="utf-8"))
                    for item in previous.get("tasks", []):
                        if isinstance(item, dict) and isinstance(item.get("task_id"), str):
                            merged.setdefault(item["task_id"], TaskOutcome(**item))
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                    pass
            all_outcomes = sorted(merged.values(), key=lambda x: x.relative_dir.casefold())
            self._write_json(self.run_dir / "summary.json", {
                "run_id": self.run_id,
                "dataset": str(self.dataset_root),
                "options": asdict(self.options),
                "updated_at": _now(),
                "tasks": [asdict(item) for item in all_outcomes],
            })

    def _log(self, message: str) -> None:
        with self._log_lock:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        RubricRun._atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        RubricRun._atomic_write(path, text)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def audit_final_patch_payload(payload: dict[str, object]) -> list[str]:
    issues: list[str] = []
    for key in ("record_patch", "spec_patch"):
        operations = payload.get(key)
        if not isinstance(operations, list):
            issues.append(f"{key} must be a list")
            continue
        if len(operations) > 30:
            issues.append(f"{key} contains too many operations; finalization must be minimal")
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                issues.append(f"{key}[{index}] must be an object")
                continue
            op = operation.get("op")
            path = operation.get("path")
            if op not in {"add", "remove", "replace"}:
                issues.append(f"{key}[{index}].op is unsupported")
            if not isinstance(path, str) or not path.startswith("/"):
                issues.append(f"{key}[{index}].path must be a JSON Pointer")
            if op in {"add", "replace"} and "value" not in operation:
                issues.append(f"{key}[{index}] requires value")
    return issues


def _arbitration_requires_change(arbitration: dict[str, object]) -> bool:
    """Return whether the binding arbitration actually orders a modification."""
    decisions = arbitration.get("decisions")
    if not isinstance(decisions, list):
        return False
    return any(
        isinstance(item, dict) and item.get("decision") in {"accept", "modify"}
        for item in decisions
    )


def apply_json_patch(document: object, operations: object) -> dict[str, object]:
    if not isinstance(document, dict) or not isinstance(operations, list):
        raise TypeError("patch document must be an object and operations must be a list")
    for operation in operations:
        if not isinstance(operation, dict):
            raise TypeError("patch operation must be an object")
        op = operation.get("op")
        pointer = operation.get("path")
        if op not in {"add", "remove", "replace"} or not isinstance(pointer, str):
            raise ValueError("invalid patch operation")
        tokens = [_decode_pointer_token(token) for token in pointer.split("/")[1:]]
        if not tokens:
            raise ValueError("root replacement is not allowed")
        parent: object = document
        for token in tokens[:-1]:
            if isinstance(parent, dict):
                if token not in parent:
                    raise KeyError(pointer)
                parent = parent[token]
            elif isinstance(parent, list):
                parent = parent[int(token)]
            else:
                raise TypeError(f"non-container in JSON Pointer: {pointer}")
        leaf = tokens[-1]
        if isinstance(parent, dict):
            if op in {"remove", "replace"} and leaf not in parent:
                raise KeyError(pointer)
            if op == "remove":
                del parent[leaf]
            else:
                parent[leaf] = operation.get("value")
        elif isinstance(parent, list):
            if op == "add" and leaf == "-":
                parent.append(operation.get("value"))
                continue
            index = int(leaf)
            if op == "add":
                if index < 0 or index > len(parent):
                    raise IndexError(pointer)
                parent.insert(index, operation.get("value"))
            elif op == "remove":
                del parent[index]
            else:
                parent[index] = operation.get("value")
        else:
            raise TypeError(f"non-container at JSON Pointer leaf: {pointer}")
    return document


def _decode_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _input_fingerprint(root: Path, exclude_rubrics: bool) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or (exclude_rubrics and is_reference_rubric(path)):
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
