from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class OpenCodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentResponse:
    text: str
    session_ids: tuple[str, ...]
    raw_output: str


class OpenCodeClient:
    def __init__(
        self,
        project_dir: Path,
        timeout_seconds: int = 1800,
        retries: int = 2,
        logger: Callable[[str], None] | None = None,
        model_by_agent: dict[str, str] | None = None,
    ):
        self.project_dir = project_dir.resolve()
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.logger = logger
        self.model_by_agent = dict(model_by_agent or {})
        configured = os.environ.get("OPENCODE_BIN")
        self.executable = _resolve_opencode_executable(configured)

    def run_agent(self, agent: str, prompt: str, title: str) -> AgentResponse:
        command = [
            self.executable, "run", "--standalone", "--format", "json",
            "--agent", agent,
        ]
        model = self.model_by_agent.get(agent)
        if model:
            command.extend(["--model", model])
        command.extend(["--title", title, prompt])
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            self._log(
                f"调用 {agent}（{title}），尝试 {attempt + 1}/{self.retries + 1}，"
                "请等待模型返回"
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.project_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
                response = parse_event_stream(completed.stdout)
                if completed.returncode != 0:
                    detail = completed.stderr.strip() or completed.stdout[-2000:]
                    error = OpenCodeError(
                        f"OpenCode agent {agent} exited with {completed.returncode}: "
                        f"{detail}"
                    )
                    # Permission/configuration failures are deterministic. Retrying
                    # them only multiplies latency and API usage.
                    lowered = detail.casefold()
                    if "permission requested" in lowered or "user dismissed" in lowered:
                        raise error
                    # Some gateways/CLI adapters return a non-zero exit code
                    # after the model has already emitted a complete answer.
                    # Let the caller's marker, JSON and semantic validators
                    # decide whether that answer is usable instead of throwing
                    # away an otherwise valid task result.
                    if response.text.strip():
                        self._log(
                            f"{agent} 返回退出码 {completed.returncode}，但检测到模型输出；"
                            "继续执行内容校验"
                        )
                    else:
                        raise error
                if not response.text.strip():
                    raise OpenCodeError(f"OpenCode agent {agent} returned no text")
                self._log(
                    f"{agent} 已返回（{title}），耗时 {time.monotonic() - started:.1f} 秒"
                )
                return response
            except (OSError, subprocess.TimeoutExpired, OpenCodeError) as exc:
                last_error = exc
                if isinstance(exc, OpenCodeError):
                    lowered = str(exc).casefold()
                    if "permission requested" in lowered or "user dismissed" in lowered:
                        break
                if attempt >= self.retries:
                    break
                self._log(f"{agent} 本次调用失败，准备重试：{exc}")
                time.sleep(2 ** attempt)
        raise OpenCodeError(str(last_error))

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger(message)


def parse_event_stream(raw: str) -> AgentResponse:
    texts: list[str] = []
    sessions: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session = event.get("sessionID") or event.get("part", {}).get("sessionID")
        if session and session not in sessions:
            sessions.append(session)
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif isinstance(event.get("text"), str):
            texts.append(event["text"])
    return AgentResponse("\n".join(texts).strip(), tuple(sessions), raw)


def extract_marked_text(text: str, begin: str, end: str) -> str:
    pattern = re.compile(re.escape(begin) + r"\s*(.*?)\s*" + re.escape(end), re.DOTALL)
    match = pattern.search(text)
    if not match:
        raise OpenCodeError(f"Response missing required markers: {begin} ... {end}")
    return match.group(1).strip()


def extract_json(text: str, begin: str, end: str) -> dict[str, object]:
    payload = extract_marked_text(text, begin, end)
    payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload.strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as strict_error:
        # Some model gateways occasionally return literal control characters
        # inside JSON strings.  Python's non-strict parser can recover that
        # unambiguously; structural errors (unescaped quotes, truncation, etc.)
        # still fail and are handled by the caller's bounded format retry.
        try:
            value = json.loads(payload, strict=False)
        except json.JSONDecodeError as exc:
            raise OpenCodeError(f"Invalid JSON response: {exc}") from strict_error
    if not isinstance(value, dict):
        raise OpenCodeError("Expected a JSON object")
    return value


def _resolve_opencode_executable(configured: str | None = None) -> str:
    """Prefer the native executable over npm's .cmd wrapper on Windows.

    The authoring prompts are intentionally detailed and can exceed cmd.exe's
    command-line limit. Calling the native executable also avoids shell quoting
    ambiguity for Chinese paths and prompt text.
    """
    candidate = configured or shutil.which("opencode.exe") or shutil.which("opencode")
    if not candidate:
        return "opencode"

    path = Path(candidate)
    if os.name == "nt" and path.suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        native = path.parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
        if native.is_file():
            return str(native)
    return str(path)
