from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
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
    terminal_error: str | None = None


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
        self.run_mode = os.environ.get("OPENCODE_RUN_MODE", "standalone").casefold()
        if self.run_mode not in {"standalone", "legacy"}:
            raise OpenCodeError("OPENCODE_RUN_MODE must be standalone or legacy")
        self.process_env = os.environ.copy()
        if self.run_mode == "legacy":
            # CLI versions have incompatible databases. Never let a legacy
            # child migrate or overwrite the user's existing OpenCode state.
            runtime = self.project_dir / ".rubric-generator" / "opencode-legacy"
            for variable, directory in (("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
                                        ("XDG_CACHE_HOME", "cache"), ("XDG_CONFIG_HOME", "config")):
                location = runtime / directory
                location.mkdir(parents=True, exist_ok=True)
                self.process_env[variable] = str(location)

    def run_agent(self, agent: str, prompt: str, title: str) -> AgentResponse:
        command = [
            self.executable, "run", "--format", "json",
            "--agent", agent,
        ]
        if self.run_mode == "standalone":
            command.append("--standalone")
        model = self.model_by_agent.get(agent)
        if model:
            if self.run_mode == "legacy" and "#" in model:
                model, variant = model.rsplit("#", 1)
                command.extend(["--variant", variant])
            command.extend(["--model", model])
        command.extend(["--title", title])
        if len(prompt) > 16_000:
            prompt_dir = self.project_dir / ".rubric-generator" / "prompt-inputs"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            prompt_file = prompt_dir / f"{digest}.txt"
            if not prompt_file.is_file():
                prompt_file.write_text(prompt, encoding="utf-8", newline="\n")
            relative_prompt = prompt_file.relative_to(self.project_dir).as_posix()
            command.extend([
                "--file", relative_prompt,
                "--",
                "完整读取所附 UTF-8 提示文件并严格执行其中全部要求。若读取结果截断，"
                "用 read 的 offset/limit 分段继续至文件末尾；不要写入系统 Temp 或扫描其他文件。"
                "不要复述提示，只输出其要求的最终结果。",
            ])
            self._log(f"{agent} 提示较长，已改用 --file 传递：{relative_prompt}")
        else:
            command.append(prompt)
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
                    env=self.process_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
                response = parse_event_stream(completed.stdout)
                if (_ended_at_output_limit(completed.stdout)
                    and not _has_complete_marked_payload(response.text)
                    and response.session_ids and attempt < self.retries):
                    self._save_diagnostics(agent, title, prompt, completed.stdout, "length", attempt)
                    if "--session" not in command:
                        command[2:2] = ["--session", response.session_ids[0]]
                    command[-1] += (
                        "\n上次输出因长度上限截断。复用当前会话已核验的证据，不重复研究；"
                        "重新输出本阶段完整标记 JSON，精简重复措辞和长引用，保留全部必需字段、"
                        "评分规则、事实、来源定位与允许差异，不续写半截 JSON。"
                    )
                    self._log(f"{agent} 输出达到长度上限，将复用会话重新返回精简完整结果")
                    continue
                if response.terminal_error and not _has_complete_marked_payload(response.text):
                    raise OpenCodeError(
                        f"OpenCode agent {agent} provider failed before complete output: "
                        f"{response.terminal_error}"
                    )
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
                if isinstance(exc, subprocess.TimeoutExpired):
                    raw = exc.stdout or ""
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    partial = parse_event_stream(raw)
                    self._save_diagnostics(agent, title, prompt, raw, "timeout", attempt)
                    if _has_complete_marked_payload(partial.text):
                        self._log(f"{agent} 超时前已返回完整标记内容，继续由调用方校验")
                        return partial
                    last_error = OpenCodeError(f"OpenCode agent {agent} timed out after {self.timeout_seconds}s; partial events saved")
                    if partial.session_ids and attempt < self.retries:
                        if "--session" not in command:
                            # Options must precede --, which terminates option parsing.
                            command[2:2] = ["--session", partial.session_ids[0]]
                            command[-1] += (
                                "\n上次调用因外部超时中断。继续已有会话，复用已完成的素材读取和核验，"
                                "不要重复已完成工作；最终仍须输出本阶段要求的完整标记 JSON。"
                            )
                        self._log(f"{agent} 将在已有会话继续；不丢弃已完成的工具核验")
                if isinstance(exc, OpenCodeError):
                    lowered = str(exc).casefold()
                    if "permission requested" in lowered or "user dismissed" in lowered:
                        break
                if attempt >= self.retries:
                    break
                self._log(f"{agent} 本次调用失败，准备重试：{last_error}")
                time.sleep(2 ** attempt)
        raise OpenCodeError(str(last_error))

    def check_tool_roundtrip(self) -> None:
        """Fail fast unless the configured gateway can continue after a tool result."""
        probe_dir = self.project_dir / ".rubric-generator" / "runtime-probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_hex(12)
        probe_file = probe_dir / f"tool-roundtrip-{secrets.token_hex(8)}.txt"
        probe_file.write_text(nonce + "\n", encoding="utf-8", newline="\n")
        relative = probe_file.relative_to(self.project_dir).as_posix()
        prompt = (
            f"必须先使用 read 工具读取 `{relative}`，然后只输出：\n"
            "BEGIN_RUNTIME_PROBE\n"
            '{"nonce":"文件中的原文"}\n'
            "END_RUNTIME_PROBE\n"
            "不得猜测文件内容，不得调用其他工具。"
        )
        response = self.run_agent("rubric-runtime-probe", prompt, "runtime tool roundtrip probe")
        payload = extract_json(response.text, "BEGIN_RUNTIME_PROBE", "END_RUNTIME_PROBE")
        if payload.get("nonce") != nonce or not _used_completed_read_tool(response.raw_output):
            raise OpenCodeError(
                "运行前工具续写检查失败：模型未完成 read 工具调用后的有效续写；"
                "已停止批量任务，未消耗题目生成调用"
            )

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger(message)

    def _save_diagnostics(self, agent: str, title: str, prompt: str, raw: str,
                          reason: str, attempt: int) -> None:
        directory = self.project_dir / ".rubric-generator" / "call-diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256((agent + title + prompt).encode("utf-8")).hexdigest()[:20]
        for key, value in self.process_env.items():
            if value and len(value) >= 8 and key.upper().endswith(("API_KEY", "TOKEN", "SECRET")):
                raw = raw.replace(value, "<redacted>")
        (directory / f"{identity}.{reason}-{attempt:02d}.jsonl").write_text(raw, encoding="utf-8")


def _ended_at_output_limit(raw: str) -> bool:
    last_reason = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") in {"step_finish", "step-finish"}:
            part = event.get("part")
            if isinstance(part, dict):
                last_reason = part.get("reason")
    return last_reason in {"length", "max_tokens"}


def parse_event_stream(raw: str) -> AgentResponse:
    texts: list[str] = []
    sessions: list[str] = []
    terminal_error: str | None = None
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
        if event.get("type") == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            error_type = str(error.get("type") or error.get("name") or "provider.error")
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            message = str(error.get("message") or data.get("message") or "unknown provider error")
            terminal_error = f"{error_type}: {message}"
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif isinstance(event.get("text"), str):
            texts.append(event["text"])
    return AgentResponse("\n".join(texts).strip(), tuple(sessions), raw, terminal_error)


def _has_complete_marked_payload(text: str) -> bool:
    for match in re.finditer(r"BEGIN_([A-Z0-9_]+)", text):
        if f"END_{match.group(1)}" in text[match.end():]:
            return True
    return False


def _used_completed_read_tool(raw: str) -> bool:
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part")
        if not isinstance(part, dict) or part.get("tool") != "read":
            continue
        state = part.get("state")
        if isinstance(state, dict) and state.get("status") == "completed":
            return True
    return False


def extract_marked_text(text: str, begin: str, end: str) -> str:
    pattern = re.compile(re.escape(begin) + r"\s*(.*?)\s*" + re.escape(end), re.DOTALL)
    match = pattern.search(text)
    if not match:
        raise OpenCodeError(f"Response missing required markers: {begin} ... {end}")
    return match.group(1).strip()


def extract_json(text: str, begin: str, end: str) -> dict[str, object]:
    try:
        payload = extract_marked_text(text, begin, end)
    except OpenCodeError:
        # A complete standalone object is unambiguous even when the model
        # omits its transport wrapper. Partial wrappers and surrounding prose
        # remain errors; callers still apply every schema/semantic validator.
        if begin in text or end in text:
            raise
        payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE).strip()
        if not (payload.startswith("{") and payload.endswith("}")):
            raise
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
