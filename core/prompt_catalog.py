from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
CATALOG_NAME = "catalog.json"
PLACEHOLDER = re.compile(r"\{\{([a-z0-9_]+)\}\}")


class PromptError(ValueError):
    """A prompt could not be rendered, so nothing may be sent to the model."""


def load_catalog() -> dict[str, Any]:
    path = PROMPTS_DIR / CATALOG_NAME
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PromptError(f"prompt catalog is unreadable: {path}: {error}") from error
    prompts = catalog.get("prompts")
    if not isinstance(prompts, dict) or not prompts:
        raise PromptError(f"prompt catalog declares no prompts: {path}")
    return catalog


def entry(prompt_id: str) -> dict[str, Any]:
    declared = load_catalog()["prompts"].get(prompt_id)
    if not isinstance(declared, dict):
        raise PromptError(f"unknown prompt id: {prompt_id}")
    return declared


def render(prompt_id: str, *, trigger: str, variables: dict[str, Any] | None = None) -> str:
    """Render one catalog prompt for one declared trigger, or refuse."""
    declared = entry(prompt_id)
    if trigger not in declared["allowed_triggers"]:
        raise PromptError(f"{prompt_id} is not allowed for trigger {trigger}")

    required = set(declared["required_variables"])
    supplied = set(variables or {})
    if required != supplied:
        raise PromptError(
            f"{prompt_id} needs exactly {sorted(required)}; "
            f"missing {sorted(required - supplied)}, unexpected {sorted(supplied - required)}"
        )

    path = PROMPTS_DIR / declared["template_file"]
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromptError(f"{prompt_id} template is unreadable: {path}: {error}") from error

    # Checking the template rather than the output is what makes an unresolved
    # placeholder impossible: every name the template uses is required, and every
    # required name is supplied. Scanning the output instead would reject a task
    # whose own text happens to contain braces.
    declared_names = set(PLACEHOLDER.findall(template))
    if declared_names != required:
        raise PromptError(
            f"{prompt_id} template and catalog disagree: template uses {sorted(declared_names)}, "
            f"catalog requires {sorted(required)}"
        )

    rendered = PLACEHOLDER.sub(lambda match: str((variables or {})[match.group(1)]), template)
    # A template file ends with a newline the way every text file does; the prompt
    # itself does not include it.
    return rendered[:-1] if rendered.endswith("\n") else rendered


def tool_descriptions() -> dict[str, str]:
    path = PROMPTS_DIR / load_catalog()["tool_descriptions_file"]
    try:
        descriptions = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PromptError(f"tool descriptions are unreadable: {path}: {error}") from error
    if not isinstance(descriptions, dict) or not all(
        isinstance(value, str) and value.strip() for value in descriptions.values()
    ):
        raise PromptError(f"tool descriptions must map every tool to a non-empty string: {path}")
    return descriptions
