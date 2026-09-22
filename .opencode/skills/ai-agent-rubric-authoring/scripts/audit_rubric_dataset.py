#!/usr/bin/env python3
"""Conservative read-only static audit. Passing does not prove task fidelity."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

FORBIDDEN = ["Success Rate", "锁定评分标准", "产品A", "产品B"]
CORE_CONCEPTS = [
    ("task/evidence boundary", r"任务|目标|证据|来源"),
    ("scoring contract", r"计分|评分|状态"),
    ("evaluation record", r"评测记录|评价记录|证据记录"),
]
ATOM_ID_RE = re.compile(r"[A-Z][A-Z0-9_-]*\d+")
WEIGHT_RE = re.compile(r"-?\d+(?:\.\d+)?")
FUNCTION_RE = re.compile(r"\b(?:BIN|RATIO|COUNT|CLAIM-RATIO)\b|状态\s*=")
VAGUE_RE = re.compile(r"酌情|较好|视情况|合理|充分|清晰|重要|代表性|适当|优质|全面|深入")
LAYER_ALIASES = {
    "objective-verifiable", "objective_verifiable", "可客观核验", "客观核验",
    "allowed-variation", "allowed_variation", "允许差异",
}


def parse_atom_rows(text: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        body = line[1:-1] if line.endswith("|") else line[1:]
        cells = [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)]
        if not cells or not ATOM_ID_RE.fullmatch(cells[0]):
            continue
        if len(cells) >= 3 and WEIGHT_RE.fullmatch(cells[1]):
            rows.append((cells[0], cells[1], cells[2]))
        elif len(cells) >= 4 and WEIGHT_RE.fullmatch(cells[2]):
            rows.append((cells[0], cells[2], cells[3]))
    return rows


def audit(path: Path) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    blockers: list[str] = []
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
        blockers.append("no scored atoms matched the required scoring-table contract")
        return blockers, advisories
    ids = [row[0] for row in rows]
    if len(ids) != len(set(ids)):
        blockers.append("duplicate scored atom ID")
    for atom, weight, condition in rows:
        if float(weight) <= 0:
            blockers.append(f"{atom} has non-positive weight")
        if not FUNCTION_RE.search(condition):
            advisories.append(f"{atom}: named deterministic state function was not detected; confirm the prose rule is computable")
        vague = sorted(set(VAGUE_RE.findall(condition)))
        if vague and not re.search(r"定义|即|是指|满足.*条件|具体为", condition):
            advisories.append(f"{atom}: confirm these terms are operationalized: {','.join(vague)}")

    groups: dict[str, float] = {}
    for atom, weight, _ in rows:
        prefix = re.match(r"[A-Z][A-Z0-9_-]*?(?=\d{2}$)", atom)
        if prefix:
            groups[prefix.group(0)] = groups.get(prefix.group(0), 0.0) + float(weight)
    for prefix, total in groups.items():
        if abs(total - 100.0) > 1e-9:
            advisories.append(f"{prefix} weights sum to {total}; confirm the rubric's own normalization")
    if not re.search(r"文件|页|表|单元格|幻灯片|段|章节|代码|URL|locator|定位", text, re.I):
        advisories.append("evidence-locator convention was not detected automatically")
    return blockers, advisories


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    problems = list(args.root.rglob("问题描述.txt"))
    rubrics = list(args.root.rglob("rubric.md"))
    failed = False
    advisory_count = 0
    if len(rubrics) != len(problems):
        print(f"BLOCKING count: problems={len(problems)} rubrics={len(rubrics)}")
        failed = True
    for rubric in rubrics:
        blockers, advisories = audit(rubric)
        if not blockers:
            print(f"USABLE {rubric}: no blocking static error detected")
        for issue in blockers:
            print(f"BLOCKING {rubric}: {issue}")
            failed = True
        for advisory in advisories:
            print(f"ADVISORY {rubric}: {advisory}")
            advisory_count += 1
    if not failed:
        print(f"OK: {len(rubrics)} rubric.md files have no blocking static errors")
    print(f"NOTE: advisories={advisory_count}; advisories do not require revision")
    print("NOTE: audit is read-only and does not validate source anchors, threshold provenance, or task fit")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
