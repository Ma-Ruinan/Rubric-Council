from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .audit import atom_statistics
from .dataset import DatasetTask, discover_tasks, is_reference_rubric


@dataclass(frozen=True)
class RegressionWorkspace:
    run_id: str
    root: Path
    dataset: Path
    references: Path
    tasks: tuple[DatasetTask, ...]


def prepare_regression(
    project_dir: Path,
    source_root: Path,
    limit: int | None = None,
    task_pattern: str | None = None,
    run_id: str | None = None,
) -> RegressionWorkspace:
    source = source_root.resolve()
    selected = discover_tasks(source)
    if task_pattern:
        needle = task_pattern.casefold()
        selected = [task for task in selected if needle in str(task.relative_dir).casefold()]
    if limit is not None:
        selected = selected[: max(0, limit)]
    if not selected:
        raise RuntimeError("回归筛选后没有题目")

    identifier = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    root = project_dir.resolve() / "regression" / "runs" / identifier
    dataset = root / "dataset"
    references = root / "references"
    for task in selected:
        target = dataset / task.relative_dir
        reference_target = references / task.relative_dir / "rubric.md"
        target.mkdir(parents=True, exist_ok=True)
        for file in sorted(task.task_dir.rglob("*"), key=lambda p: p.as_posix().casefold()):
            if not file.is_file():
                continue
            if is_reference_rubric(file):
                if file.name.casefold() == "rubric.md":
                    reference_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, reference_target)
                continue
            output = target / file.relative_to(task.task_dir)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, output)

    copied_tasks = tuple(discover_tasks(dataset))
    manifest = {
        "run_id": identifier,
        "source_root": str(source),
        "dataset": str(dataset),
        "references": str(references),
        "tasks": [str(task.relative_dir) for task in copied_tasks],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return RegressionWorkspace(identifier, root, dataset, references, copied_tasks)


def compare_regression(workspace: RegressionWorkspace) -> dict[str, object]:
    comparisons: list[dict[str, object]] = []
    for task in workspace.tasks:
        generated = task.rubric_file
        reference = workspace.references / task.relative_dir / "rubric.md"
        item: dict[str, object] = {
            "task": str(task.relative_dir),
            "generated_exists": generated.is_file(),
            "reference_exists": reference.is_file(),
        }
        if generated.is_file():
            item["generated"] = atom_statistics(generated.read_text(encoding="utf-8"))
        if reference.is_file():
            item["reference"] = atom_statistics(reference.read_text(encoding="utf-8"))
        comparisons.append(item)
    result = {"run_id": workspace.run_id, "tasks": comparisons}
    (workspace.root / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
