from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MODEL = "aiaaa/deepseek-v4.1-flash#high"
AGENT_ROLES = (
    "rubric-author",
    "rubric-reviewer",
    "rubric-review-classifier",
    "rubric-arbitrator",
    "rubric-orchestrator",
)


@dataclass(frozen=True)
class ProjectSettings:
    agent_models: dict[str, str]

    def model_for(self, agent: str) -> str | None:
        return self.agent_models.get(agent)


def load_project_env(project_dir: Path) -> tuple[str, ...]:
    """Load simple KEY=VALUE entries from the ignored project .env file.

    Existing process variables always win.  Values are never returned or
    logged; the returned tuple contains only variable names.
    """
    path = project_dir.resolve() / ".env"
    if not path.is_file():
        return ()
    loaded: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f".env line {line_number} must use KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
            raise ValueError(f".env line {line_number} has an invalid variable name")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


def load_project_settings(project_dir: Path) -> ProjectSettings:
    path = project_dir.resolve() / "rubric-generator.toml"
    models = {role: DEFAULT_MODEL for role in AGENT_ROLES}
    if not path.is_file():
        return ProjectSettings(models)

    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    agents = payload.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError("rubric-generator.toml: [agents] must be a table")
    for role in AGENT_ROLES:
        value = agents.get(role)
        if value is None:
            continue
        if not isinstance(value, dict) or not isinstance(value.get("model"), str):
            raise ValueError(f"rubric-generator.toml: agents.{role}.model must be a string")
        model = value["model"].strip()
        if not model or "/" not in model:
            raise ValueError(f"rubric-generator.toml: invalid model for {role}")
        models[role] = model
    return ProjectSettings(models)
