from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
CATALOG_NAME = "catalog.json"
VARIABLE_NAME = re.compile(r"[a-z][a-z0-9_]*")


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
    surface = declared.get("injection_surface")
    if not isinstance(surface, str) or not surface.strip():
        raise PromptError(f"{prompt_id} must declare a non-empty injection_surface")
    triggers = declared.get("allowed_triggers")
    if not isinstance(triggers, list) or not triggers or any(
        not isinstance(item, str) or not item.strip() for item in triggers
    ):
        raise PromptError(f"{prompt_id} must declare a non-empty list of allowed_triggers")
    variables = declared.get("required_variables")
    if not isinstance(variables, list) or len(set(variables)) != len(variables) or any(
        not isinstance(item, str) or not VARIABLE_NAME.fullmatch(item) for item in variables
    ):
        raise PromptError(f"{prompt_id} required_variables must be distinct lower_snake_case names")
    if not _template_path(prompt_id, declared.get("template_file")).is_file():
        raise PromptError(f"{prompt_id} template_file must name a file inside {PROMPTS_DIR}")
    return declared


def render(
    prompt_id: str,
    *,
    surface: str,
    trigger: str,
    variables: dict[str, Any] | None = None,
) -> str:
    """Render one catalog prompt onto one declared surface for one declared trigger."""
    declared = entry(prompt_id)
    if surface != declared["injection_surface"]:
        raise PromptError(
            f"{prompt_id} is injected on {declared['injection_surface']}, not {surface}"
        )
    if trigger not in declared["allowed_triggers"]:
        raise PromptError(f"{prompt_id} is not allowed for trigger {trigger}")

    required = set(declared["required_variables"])
    supplied = set(variables or {})
    if required != supplied:
        raise PromptError(
            f"{prompt_id} needs exactly {sorted(required)}; "
            f"missing {sorted(required - supplied)}, unexpected {sorted(supplied - required)}"
        )

    path = _template_path(prompt_id, declared["template_file"])
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromptError(f"{prompt_id} template is unreadable: {path}: {error}") from error

    spans = _placeholders(prompt_id, template)
    if {name for _, _, name in spans} != required:
        raise PromptError(
            f"{prompt_id} template and catalog disagree: template uses "
            f"{sorted({name for _, _, name in spans})}, catalog requires {sorted(required)}"
        )

    rendered = _substitute(template, spans, variables or {})
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


def _template_path(prompt_id: str, template_file: Any) -> Path:
    if not isinstance(template_file, str) or not template_file or Path(template_file).is_absolute():
        raise PromptError(f"{prompt_id} template_file must be a path relative to {PROMPTS_DIR}")
    root = PROMPTS_DIR.resolve()
    path = (root / template_file).resolve()
    if not path.is_relative_to(root):
        raise PromptError(f"{prompt_id} template_file escapes {PROMPTS_DIR}")
    return path


def _placeholders(prompt_id: str, template: str) -> list[tuple[int, int, str]]:
    """Locate every placeholder, refusing braces a renderer could not resolve.

    Only the template is read. Scanning the rendered output instead would reject a
    task whose own description happens to contain double braces.
    """
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    while (opened := template.find("{{", cursor)) >= 0:
        if "}}" in template[cursor:opened]:
            raise PromptError(f"{prompt_id} template closes a placeholder it never opened")
        closed = template.find("}}", opened + 2)
        if closed < 0:
            raise PromptError(f"{prompt_id} template opens a placeholder it never closes")
        name = template[opened + 2 : closed]
        if not VARIABLE_NAME.fullmatch(name):
            raise PromptError(
                f"{prompt_id} template placeholder {{{{{name}}}}} is not a lower_snake_case name"
            )
        spans.append((opened, closed + 2, name))
        cursor = closed + 2
    if "}}" in template[cursor:]:
        raise PromptError(f"{prompt_id} template closes a placeholder it never opened")
    return spans


def _substitute(template: str, spans: list[tuple[int, int, str]], variables: dict[str, Any]) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end, name in spans:
        pieces.append(template[cursor:start])
        pieces.append(str(variables[name]))
        cursor = end
    pieces.append(template[cursor:])
    return "".join(pieces)
