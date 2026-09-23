from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import audit_dataset
from .dataset import discover_tasks
from .regression import compare_regression, prepare_regression
from .runner import RubricRun, RunOptions


def _project_default() -> Path:
    return Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rubric-generator")
    parser.add_argument("--project-dir", type=Path, default=_project_default())
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="审计已有数据集")
    audit.add_argument("--dataset", type=Path, required=True)

    generate = sub.add_parser("generate", help="为数据集生成 Rubric")
    generate.add_argument("--dataset", type=Path, required=True)
    generate.add_argument("--concurrency", type=int, default=3, choices=(1, 2, 3))
    generate.add_argument("--force", action="store_true")
    generate.add_argument("--task-pattern")
    generate.add_argument("--missing-only", action="store_true", help="只处理尚无 rubric.md 的题目")
    generate.add_argument("--limit", type=int)
    generate.add_argument("--timeout", type=int, default=900)

    resume = sub.add_parser("resume", help="从已有运行的已校验检查点继续")
    resume.add_argument("--dataset", type=Path, required=True)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--concurrency", type=int, default=1, choices=(1, 2, 3))
    resume.add_argument("--task-pattern")
    resume.add_argument("--missing-only", action="store_true", help="只处理尚无 rubric.md 的题目")
    resume.add_argument("--limit", type=int)
    resume.add_argument("--timeout", type=int, default=900)

    regression = sub.add_parser("regression", help="复制只读参考数据集并运行隔离回归")
    regression.add_argument("--source", type=Path, required=True)
    regression.add_argument("--concurrency", type=int, default=1, choices=(1, 2, 3))
    regression.add_argument("--task-pattern")
    regression.add_argument("--limit", type=int, default=1)
    regression.add_argument("--timeout", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = args.project_dir.resolve()

    if args.command == "audit":
        results = audit_dataset(args.dataset)
        failures = 0
        advisory_count = 0
        for result in results:
            if result.passed:
                print(f"USABLE {result.path}: no blocking static error detected")
            else:
                failures += 1
                for issue in result.issues:
                    print(f"BLOCKING {result.path}: {issue}")
            for advisory in result.advisories:
                advisory_count += 1
                print(f"ADVISORY {result.path}: {advisory}")
        print(
            f"Audited {len(results)} rubric files; usable={len(results) - failures}; "
            f"blocked={failures}; advisories={advisory_count}"
        )
        print("Audit is read-only. Advisories do not require revision; blocking findings must be reviewed before any edit.")
        return 1 if failures else 0

    if args.command in {"generate", "resume"}:
        tasks = discover_tasks(args.dataset)
        if args.task_pattern:
            needle = args.task_pattern.casefold()
            tasks = [task for task in tasks if needle in str(task.relative_dir).casefold()]
        if args.missing_only:
            tasks = [task for task in tasks if not task.rubric_file.is_file()]
        if args.limit is not None:
            tasks = tasks[: max(0, args.limit)]
        is_resume = args.command == "resume"
        run = RubricRun(project, args.dataset, RunOptions(
            concurrency=args.concurrency,
            force=True if is_resume else args.force,
            timeout_seconds=args.timeout,
            resume=is_resume,
        ), run_id=args.run_id if is_resume else None)
        try:
            outcomes = run.execute(tasks)
        except RuntimeError as exc:
            print(f"运行已安全停止：{exc}", file=sys.stderr, flush=True)
            return 1
        print(json.dumps([outcome.__dict__ for outcome in outcomes], ensure_ascii=False, indent=2))
        return 0 if all(item.status in {"COMPLETED", "AUTO_FINALIZED", "SKIPPED"} for item in outcomes) else 1

    print(f"正在准备隔离回归工作区：{args.source.resolve()}", flush=True)
    workspace = prepare_regression(
        project, args.source, args.limit, args.task_pattern
    )
    print(
        f"已复制 {len(workspace.tasks)} 道题；不会改动源数据集。工作区：{workspace.root}",
        flush=True,
    )
    run = RubricRun(project, workspace.dataset, RunOptions(
        concurrency=args.concurrency,
        force=False,
        timeout_seconds=args.timeout,
    ), run_id=f"regression-{workspace.run_id}")
    try:
        outcomes = run.execute(list(workspace.tasks))
    except RuntimeError as exc:
        print(f"运行已安全停止：{exc}", file=sys.stderr, flush=True)
        return 1
    comparison = compare_regression(workspace)
    print(json.dumps({
        "workspace": str(workspace.root),
        "outcomes": [item.__dict__ for item in outcomes],
        "comparison": comparison,
    }, ensure_ascii=False, indent=2))
    return 0 if all(item.status in {"COMPLETED", "AUTO_FINALIZED"} for item in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
