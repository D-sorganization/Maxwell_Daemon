"""Goose-style YAML recipes — named, shareable agent workflows.

A *recipe* bundles:

* an instruction prompt (the system/user text sent to the model),
* a declared parameter list (typed, with required/default/description),
* a tool allow/deny list (what the agent may call during this workflow),
* routing hints (``requires``: preferred model tier, max turns, budget).

Recipes live in ``.maxwell/recipes/*.yaml``. The loader is lenient at the
*directory* level (one broken file doesn't kill a sweep) but strict at the
*file* level (bad schema → :class:`RecipeLoadError` so the authoring tool
can surface the problem).

DbC: the parser is pure (bytes in, value out). ``bind_parameters`` is the
precondition gate for every invocation — it refuses unknown, mistyped, or
missing-required inputs via :class:`PreconditionError` so downstream steps
can assume clean, fully-populated parameter dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from maxwell_daemon.contracts import require
from maxwell_daemon.logging import get_logger

__all__ = [
    "Recipe",
    "RecipeLoadError",
    "RecipeParameter",
    "RecipeRequires",
    "RecipeTools",
    "bind_parameters",
    "load_recipe",
    "load_recipe_directory",
    "render_instructions",
]

_LOG = get_logger(__name__)

_SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})
_PARAM_TYPES: frozenset[str] = frozenset({"string", "integer", "number", "boolean"})
ParamType = Literal["string", "integer", "number", "boolean"]

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class RecipeLoadError(ValueError):
    """Raised when a recipe YAML file doesn't parse into a valid :class:`Recipe`."""


@dataclass(slots=True, frozen=True)
class RecipeParameter:
    """One declared parameter for a recipe."""

    name: str
    type: ParamType
    description: str
    required: bool = False
    default: Any = None


@dataclass(slots=True, frozen=True)
class RecipeTools:
    """Tool allow/deny list. Empty ``allow`` means allow-any; ``deny`` always applied."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class RecipeRequires:
    """Routing hints for the dispatch layer — not enforced by the recipe itself."""

    model_tier: str | None = None
    max_turns: int | None = None
    budget_per_story_usd: float | None = None


@dataclass(slots=True, frozen=True)
class Recipe:
    """A parsed YAML recipe — one named agent workflow."""

    name: str
    description: str
    version: int
    instructions: str
    parameters: tuple[RecipeParameter, ...]
    tools: RecipeTools
    requires: RecipeRequires
    source: Path


# ── Loaders ─────────────────────────────────────────────────────────────────


def load_recipe(path: Path) -> Recipe:
    """Parse one YAML recipe file into a :class:`Recipe`.

    Raises :class:`RecipeLoadError` on missing required fields, unknown
    parameter types, unsupported schema versions, or malformed tools lists.
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RecipeLoadError(f"{path}: invalid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise RecipeLoadError(f"{path}: top-level must be a mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise RecipeLoadError(f"{path}: missing or empty 'name'")

    description = raw.get("description")
    if not isinstance(description, str) or not description:
        raise RecipeLoadError(f"{path}: missing or empty 'description'")

    version_raw = raw.get("version", 1)
    if not isinstance(version_raw, int) or isinstance(version_raw, bool):
        raise RecipeLoadError(f"{path}: 'version' must be an integer")
    if version_raw not in _SUPPORTED_VERSIONS:
        raise RecipeLoadError(
            f"{path}: unsupported 'version' {version_raw!r} "
            f"(supported: {sorted(_SUPPORTED_VERSIONS)})"
        )

    instructions = raw.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise RecipeLoadError(f"{path}: missing or empty 'instructions'")

    parameters = _parse_parameters(path, raw.get("parameters"))
    tools = _parse_tools(path, raw.get("tools"))
    requires = _parse_requires(path, raw.get("requires"))

    return Recipe(
        name=name,
        description=description,
        version=version_raw,
        instructions=instructions,
        parameters=parameters,
        tools=tools,
        requires=requires,
        source=path,
    )


def load_recipe_directory(directory: Path) -> tuple[Recipe, ...]:
    """Load every ``*.yaml`` / ``*.yml`` recipe under ``directory`` (non-recursive).

    Missing directory → empty tuple. Files that fail to parse are skipped
    with a WARNING-level log entry (no exception) so one bad recipe never
    kills the whole sweep.
    """
    if not directory.is_dir():
        return ()
    out: list[Recipe] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
            continue
        try:
            out.append(load_recipe(path))
        except RecipeLoadError as e:
            _LOG.warning("skipping malformed recipe %s: %s", path, e)
            continue
    return tuple(out)


def _parse_parameters(path: Path, raw: Any) -> tuple[RecipeParameter, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise RecipeLoadError(f"{path}: 'parameters' must be a mapping")
    out: list[RecipeParameter] = []
    for name, spec in raw.items():
        if not isinstance(name, str):
            raise RecipeLoadError(f"{path}: parameter names must be strings")
        if not isinstance(spec, dict):
            raise RecipeLoadError(f"{path}: parameter {name!r} must be a mapping")
        ptype = spec.get("type")
        if ptype not in _PARAM_TYPES:
            raise RecipeLoadError(
                f"{path}: parameter {name!r} has unknown 'type' {ptype!r} "
                f"(expected one of {sorted(_PARAM_TYPES)})"
            )
        description = spec.get("description", "")
        if not isinstance(description, str):
            raise RecipeLoadError(f"{path}: parameter {name!r} 'description' must be a string")
        required = spec.get("required", False)
        if not isinstance(required, bool):
            raise RecipeLoadError(f"{path}: parameter {name!r} 'required' must be a boolean")
        default = spec.get("default")
        out.append(
            RecipeParameter(
                name=name,
                type=ptype,
                description=description,
                required=required,
                default=default,
            )
        )
    return tuple(out)


def _parse_tools(path: Path, raw: Any) -> RecipeTools:
    if raw is None:
        return RecipeTools()
    if not isinstance(raw, dict):
        raise RecipeLoadError(f"{path}: 'tools' must be a mapping")
    allow = raw.get("allow", [])
    deny = raw.get("deny", [])
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise RecipeLoadError(f"{path}: 'tools.allow' must be a list of strings")
    if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
        raise RecipeLoadError(f"{path}: 'tools.deny' must be a list of strings")
    return RecipeTools(allow=tuple(allow), deny=tuple(deny))


def _parse_requires(path: Path, raw: Any) -> RecipeRequires:
    if raw is None:
        return RecipeRequires()
    if not isinstance(raw, dict):
        raise RecipeLoadError(f"{path}: 'requires' must be a mapping")
    model_tier = raw.get("model_tier")
    if model_tier is not None and not isinstance(model_tier, str):
        raise RecipeLoadError(f"{path}: 'requires.model_tier' must be a string")
    max_turns = raw.get("max_turns")
    if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool)):
        raise RecipeLoadError(f"{path}: 'requires.max_turns' must be an integer")
    budget = raw.get("budget_per_story_usd")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int | float)):
        # bool is a subclass of int in Python — we don't want ``True`` to
        # silently become 1.0, so check bool first.
        raise RecipeLoadError(f"{path}: 'requires.budget_per_story_usd' must be a number")
    return RecipeRequires(
        model_tier=model_tier,
        max_turns=max_turns,
        budget_per_story_usd=float(budget) if budget is not None else None,
    )


# ── Parameter binding & templating ──────────────────────────────────────────


def bind_parameters(recipe: Recipe, *, supplied: dict[str, Any]) -> dict[str, Any]:
    """Resolve concrete values for every declared parameter.

    * Missing required param → :class:`PreconditionError`.
    * Type mismatch (e.g. int where string declared) → :class:`PreconditionError`.
    * Unknown supplied name → :class:`PreconditionError` (never silently ignored).
    * Missing optional → fill from ``default`` if present, else omit.

    Returns the fully-resolved parameter dict ready for instruction templating.
    """
    declared = {p.name: p for p in recipe.parameters}

    extra = set(supplied) - set(declared)
    require(
        not extra,
        f"recipe {recipe.name!r}: extra supplied parameter(s) {sorted(extra)!r} "
        f"not declared in the recipe",
    )

    resolved: dict[str, Any] = {}
    for name, spec in declared.items():
        if name in supplied:
            value = supplied[name]
            require(
                _type_matches(value, spec.type),
                f"recipe {recipe.name!r}: parameter {name!r} expected type "
                f"{spec.type!r}, got {type(value).__name__}",
            )
            resolved[name] = value
        elif spec.required:
            require(
                False,
                f"recipe {recipe.name!r}: required parameter {name!r} not supplied",
            )
        elif spec.default is not None:
            resolved[name] = spec.default
    return resolved


def _type_matches(value: Any, declared: ParamType) -> bool:
    """Strict-ish type check. ``bool`` never satisfies ``integer`` or ``number``."""
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return False


def render_instructions(recipe: Recipe, bound: dict[str, Any]) -> str:
    """Substitute ``{{name}}`` placeholders in ``recipe.instructions``.

    Values are stringified with ``str(...)``. Placeholders whose name isn't
    in ``bound`` stay literal so the author can spot unresolved templates
    in their workflow output.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in bound:
            return str(bound[name])
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, recipe.instructions)
