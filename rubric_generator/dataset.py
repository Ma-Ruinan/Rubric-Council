from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


PROBLEM_FILENAME = "问题描述.txt"
RUBRIC_FILENAME = "rubric.md"


@dataclass(frozen=True)
class DatasetTask:
    dataset_root: Path
    problem_file: Path
    task_dir: Path
    relative_dir: Path
    task_id: str

    @property
    def rubric_file(self) -> Path:
        return self.task_dir / RUBRIC_FILENAME


def _safe_slug(text: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", text).strip("-._")
    return slug[:70] or "task"


def make_task_id(relative_dir: Path) -> str:
    normalized = relative_dir.as_posix()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{_safe_slug(relative_dir.name)}-{digest}"


def discover_tasks(dataset_root: Path) -> list[DatasetTask]:
    root = dataset_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"数据集目录不存在: {root}")
    tasks: list[DatasetTask] = []
    for problem in sorted(root.rglob(PROBLEM_FILENAME), key=lambda p: p.as_posix().casefold()):
        task_dir = problem.parent
        relative = task_dir.relative_to(root)
        tasks.append(DatasetTask(root, problem, task_dir, relative, make_task_id(relative)))
    return tasks


def is_reference_rubric(path: Path) -> bool:
    lower = path.name.casefold()
    return lower == RUBRIC_FILENAME or ("rubric" in lower and path.suffix.casefold() in {".md", ".txt"})


def copy_task_snapshot(task: DatasetTask, destination: Path) -> Path:
    """Copy one task without any existing rubric into an isolated working directory."""
    target = destination / task.relative_dir
    target.mkdir(parents=True, exist_ok=True)
    for source in sorted(task.task_dir.rglob("*"), key=lambda p: p.as_posix().casefold()):
        if not source.is_file() or is_reference_rubric(source):
            continue
        rel = source.relative_to(task.task_dir)
        output = target / rel
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    if not (target / PROBLEM_FILENAME).is_file():
        raise FileNotFoundError(f"隔离快照缺少 {PROBLEM_FILENAME}: {target}")
    return target
