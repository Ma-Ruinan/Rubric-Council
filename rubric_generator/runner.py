from __future__ import annotations

import json
import os
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
from .dataset import DatasetTask, copy_task_snapshot, discover_tasks
from .materials import prepare_material_evidence
from .opencode_client import OpenCodeClient, OpenCodeError, extract_json
from .rubric_spec import (
    audit_authoring_record_spec_alignment,
    audit_rubric_spec,
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
    max_final_compliance_repairs: int = 1
    max_format_retries: int = 1
    max_schema_retries: int = 1
    force: bool = False
    timeout_seconds: int = 900
    retries: int = 1


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
        state: dict[str, object] = {
            "task_id": task.task_id,
            "relative_dir": str(task.relative_dir),
            "status": "AUTHORING",
            "revision_rounds": 0,
            "started_at": _now(),
        }
        self._write_json(task_run / "status.json", state)

        try:
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
            })
            material_manifest = prepare_material_evidence(snapshot_dir, task_run / "materials")
            scratch_dir = task_run / "scratch"
            scratch_dir.mkdir(parents=True, exist_ok=True)

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
            revision_rounds = 0
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
                authoring_record = self._call_authoring_record(
                    task.task_id, artifacts, "author-final-record",
                    prompts.author_final_record(
                        self.project_dir, snapshot_dir, material_manifest, current_record,
                        current_spec, arbitration_path,
                    ),
                    f"finalize record {task.task_id}",
                )
                current_record = artifacts / "authoring-record-finalized.json"
                self._write_json(current_record, authoring_record)
                record_history.append(current_record)
                rubric_spec = self._call_rubric_spec(
                    task.task_id, artifacts, "author-final-spec",
                    prompts.author_final_spec(
                        self.project_dir, snapshot_dir, material_manifest, current_record,
                        current_spec, arbitration_path,
                    ),
                    f"finalize spec {task.task_id}", snapshot_dir, authoring_record,
                )
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
                    raw_compliance_data = self._call_validated_json_agent(
                        task.task_id, artifacts, f"final-review-{compliance_index + 1:02d}",
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
                    raw_compliance_path = artifacts / f"final-review-{compliance_index + 1:02d}.raw.json"
                    self._write_json(raw_compliance_path, raw_compliance_data)
                    raw_blocking = raw_compliance_data.get("blocking")
                    if raw_compliance_data.get("verdict") == "passed" and not raw_blocking:
                        compliance_data = self._apply_review_classification_gate(
                            task.task_id, artifacts, f"final-review-{compliance_index + 1:02d}",
                            raw_compliance_data, snapshot_dir, current_record, current_spec,
                            current_rubric, raw_compliance_path,
                        )
                    else:
                        compliance_data = raw_compliance_data
                    compliance_path = artifacts / f"final-review-{compliance_index + 1:02d}.json"
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
                    authoring_record = self._call_authoring_record(
                        task.task_id, artifacts, "author-final-correction-record",
                        prompts.author_final_record(
                            self.project_dir, snapshot_dir, material_manifest, current_record,
                            current_spec, arbitration_path, compliance_review=compliance_path,
                        ),
                        f"correct final record {task.task_id}",
                    )
                    current_record = artifacts / "authoring-record-final-corrected.json"
                    self._write_json(current_record, authoring_record)
                    rubric_spec = self._call_rubric_spec(
                        task.task_id, artifacts, "author-final-correction-spec",
                        prompts.author_final_spec(
                            self.project_dir, snapshot_dir, material_manifest, current_record,
                            current_spec, arbitration_path, compliance_review=compliance_path,
                        ),
                        f"correct final spec {task.task_id}", snapshot_dir, authoring_record,
                    )
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
        except (OSError, UnicodeError, OpenCodeError, ValueError) as exc:
            status = "INPUT_BLOCKED" if isinstance(exc, (OSError, UnicodeError)) else "FAILED"
            state.update({"status": status, "completed_at": _now(), "error": str(exc)})
            self._write_json(task_run / "status.json", state)
            self._log(f"[{task.task_id}] 失败：{status}；{exc}")
            return TaskOutcome(task.task_id, str(task.relative_dir), status, error=str(exc))

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
            return [
                *audit_rubric_spec(spec, task_dir),
                *audit_authoring_record_spec_alignment(record, spec),
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
        issues: list[str] = []
        for attempt in range(self.options.max_schema_retries + 1):
            suffix = "" if attempt == 0 else f".schema-retry-{attempt:02d}"
            attempt_prompt = prompt
            if attempt:
                invalid = artifacts / f"{phase}.schema-invalid-{attempt - 1:02d}.json"
                attempt_prompt += self._schema_correction_instruction(invalid, issues, payload_name)
                self._log(f"[{task_id}] {phase} 结构校验未通过，进行第 {attempt} 次纠错")
            payload = self._call_json_agent(
                task_id, artifacts, phase + suffix, agent, attempt_prompt, title + suffix,
                begin, end,
            )
            issues = validator(payload)
            if not issues:
                return payload
            self._write_json(artifacts / f"{phase}.schema-invalid-{attempt:02d}.json", payload)
        raise OpenCodeError(f"Invalid {payload_name} after bounded correction: " + "; ".join(issues))

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
                last_error = exc
        raise last_error or OpenCodeError(f"{phase} returned invalid JSON")

    def _schema_correction_instruction(self, invalid_path: Path, issues: list[str], payload_name: str) -> str:
        relative = invalid_path.resolve().relative_to(self.project_dir).as_posix()
        return (
            "\n\n上一次输出可以解析，但未通过机器结构校验。"
            f"无效的 {payload_name} 已保存于：{relative}\n"
            "只修复下列错误，不重新设计已经正确的事实、判断或评分契约：\n"
            + json.dumps(issues, ensure_ascii=False, indent=2)
            + "\n读取无效版本并输出修正后的完整对象，继续使用原提示要求的标记。"
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
            self._write_json(self.run_dir / "summary.json", {
                "run_id": self.run_id,
                "dataset": str(self.dataset_root),
                "options": asdict(self.options),
                "updated_at": _now(),
                "tasks": [asdict(item) for item in sorted(outcomes, key=lambda x: x.relative_dir.casefold())],
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
