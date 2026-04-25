"""Tests for Goose-style YAML recipes.

Recipes bundle an instruction prompt, a tool allow/deny list, declared
parameters, and routing hints — a named, shareable agent workflow. See
``maxwell_daemon/recipes.py`` for the schema and loader.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from textwrap import dedent

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.recipes import (
    Recipe,
    RecipeLoadError,
    RecipeParameter,
    RecipeRequires,
    RecipeTools,
    bind_parameters,
    load_recipe,
    load_recipe_directory,
    render_instructions,
)


def _write_recipe(tmp_path: Path, body: str, name: str = "fix-flaky-test.yaml") -> Path:
    path = tmp_path / name
    path.write_text(dedent(body).lstrip())
    return path


_FULL_RECIPE = """
    name: fix-flaky-test
    description: Investigate a flaky test and either fix it or mark xfail.
    version: 1
    instructions: |
      You are investigating a flaky test. Follow the procedure.
    parameters:
      test_path:
        type: string
        description: Path to the flaky test file
        required: true
      max_runs:
        type: integer
        default: 10
        description: How many times to run the test
    tools:
      allow: [read_file, run_bash, glob_files, grep_files]
      deny: [write_file, edit_file]
    requires:
      model_tier: complex
      max_turns: 40
"""


# ── Shape ────────────────────────────────────────────────────────────────────


class TestShapes:
    def test_recipe_frozen(self, tmp_path: Path) -> None:
        r = Recipe(
            name="x",
            description="y",
            version=1,
            instructions="go",
            parameters=(),
            tools=RecipeTools(),
            requires=RecipeRequires(),
            source=tmp_path / "x.yaml",
        )
        with pytest.raises(FrozenInstanceError):
            r.name = "z"  # type: ignore[misc]

    def test_recipe_parameter_frozen(self) -> None:
        p = RecipeParameter(name="x", type="string", description="d")
        with pytest.raises(FrozenInstanceError):
            p.name = "y"  # type: ignore[misc]

    def test_recipe_tools_frozen(self) -> None:
        t = RecipeTools()
        with pytest.raises(FrozenInstanceError):
            t.allow = ("x",)  # type: ignore[misc]

    def test_recipe_requires_frozen(self) -> None:
        rq = RecipeRequires()
        with pytest.raises(FrozenInstanceError):
            rq.model_tier = "x"  # type: ignore[misc]


# ── load_recipe ─────────────────────────────────────────────────────────────


class TestLoadRecipe:
    def test_happy_path(self, tmp_path: Path) -> None:
        path = _write_recipe(tmp_path, _FULL_RECIPE)
        r = load_recipe(path)
        assert r.name == "fix-flaky-test"
        assert r.description.startswith("Investigate")
        assert r.version == 1
        assert "investigating a flaky test" in r.instructions
        assert len(r.parameters) == 2
        names = {p.name for p in r.parameters}
        assert names == {"test_path", "max_runs"}
        test_path_param = next(p for p in r.parameters if p.name == "test_path")
        assert test_path_param.type == "string"
        assert test_path_param.required is True
        max_runs_param = next(p for p in r.parameters if p.name == "max_runs")
        assert max_runs_param.type == "integer"
        assert max_runs_param.default == 10
        assert max_runs_param.required is False
        assert r.tools.allow == ("read_file", "run_bash", "glob_files", "grep_files")
        assert r.tools.deny == ("write_file", "edit_file")
        assert r.requires.model_tier == "complex"
        assert r.requires.max_turns == 40
        assert r.source == path

    def test_missing_name_rejected(self, tmp_path: Path) -> None:
        path = _write_recipe(
            tmp_path,
            """
            description: x
            version: 1
            instructions: go
            """,
        )
        with pytest.raises(RecipeLoadError, match="name"):
            load_recipe(path)

    def test_missing_description_rejected(self, tmp_path: Path) -> None:
        path = _write_recipe(
            tmp_path,
            """
            name: x
            version: 1
            instructions: go
            """,
        )
        with pytest.raises(RecipeLoadError, match="description"):
            load_recipe(path)

    def test_missing_instructions_rejected(self, tmp_path: Path) -> None:
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            """,
        )
        with pytest.raises(RecipeLoadError, match="instructions"):
            load_recipe(path)

    def test_unknown_parameter_type_rejected(self, tmp_path: Path) -> None:
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            parameters:
              p1:
                type: uuid
                description: d
            """,
        )
        with pytest.raises(RecipeLoadError, match="type"):
            load_recipe(path)

    def test_unsupported_version_rejected(self, tmp_path: Path) -> None:
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 99
            instructions: go
            """,
        )
        with pytest.raises(RecipeLoadError, match="version"):
            load_recipe(path)

    def test_non_mapping_top_level_rejected(self, tmp_path: Path) -> None:
        path = _write_recipe(tmp_path, "- just a list\n- of things\n")
        with pytest.raises(RecipeLoadError):
            load_recipe(path)

    def test_bad_tools_structure_rejected(self, tmp_path: Path) -> None:
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            tools:
              allow: "not a list"
            """,
        )
        with pytest.raises(RecipeLoadError, match="tools"):
            load_recipe(path)


# ── load_recipe_directory ────────────────────────────────────────────────────


class TestLoadRecipeDirectory:
    def test_returns_empty_for_missing_dir(self, tmp_path: Path) -> None:
        assert load_recipe_directory(tmp_path / "nope") == ()

    def test_loads_yaml_and_yml(self, tmp_path: Path) -> None:
        _write_recipe(tmp_path, _FULL_RECIPE, name="a.yaml")
        _write_recipe(
            tmp_path,
            """
            name: other
            description: d
            version: 1
            instructions: go
            """,
            name="b.yml",
        )
        recipes = load_recipe_directory(tmp_path)
        assert sorted(r.name for r in recipes) == ["fix-flaky-test", "other"]

    def test_skips_malformed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_recipe(tmp_path, _FULL_RECIPE, name="good.yaml")
        _write_recipe(tmp_path, "not: [valid\n", name="bad.yaml")
        recipes = load_recipe_directory(tmp_path)
        assert [r.name for r in recipes] == ["fix-flaky-test"]
        captured = capsys.readouterr()
        assert "bad.yaml" in captured.out or "bad.yaml" in captured.err

    def test_ignores_non_yaml_files(self, tmp_path: Path) -> None:
        (tmp_path / "readme.md").write_text("not a recipe")
        _write_recipe(tmp_path, _FULL_RECIPE, name="a.yaml")
        recipes = load_recipe_directory(tmp_path)
        assert [r.name for r in recipes] == ["fix-flaky-test"]


# ── bind_parameters ──────────────────────────────────────────────────────────


def _make_recipe(*params: RecipeParameter, tmp_path: Path | None = None) -> Recipe:
    return Recipe(
        name="r",
        description="d",
        version=1,
        instructions="go",
        parameters=params,
        tools=RecipeTools(),
        requires=RecipeRequires(),
        source=(tmp_path or Path("/tmp")) / "r.yaml",
    )


class TestBindParameters:
    def test_required_and_optional(self) -> None:
        r = _make_recipe(
            RecipeParameter(name="a", type="string", description="", required=True),
            RecipeParameter(name="b", type="integer", description="", default=5),
        )
        bound = bind_parameters(r, supplied={"a": "hello"})
        assert bound == {"a": "hello", "b": 5}

    def test_missing_required_raises(self) -> None:
        r = _make_recipe(
            RecipeParameter(name="a", type="string", description="", required=True),
        )
        with pytest.raises(PreconditionError, match="a"):
            bind_parameters(r, supplied={})

    def test_type_mismatch_raises(self) -> None:
        r = _make_recipe(
            RecipeParameter(name="a", type="string", description="", required=True),
        )
        with pytest.raises(PreconditionError, match="string"):
            bind_parameters(r, supplied={"a": 42})

    def test_integer_accepts_int_rejects_bool(self) -> None:
        # bool is a subtype of int in Python but we don't want `True` to
        # satisfy an integer parameter silently.
        r = _make_recipe(
            RecipeParameter(name="n", type="integer", description="", required=True),
        )
        assert bind_parameters(r, supplied={"n": 7}) == {"n": 7}
        with pytest.raises(PreconditionError):
            bind_parameters(r, supplied={"n": True})

    def test_default_fills_in_optional(self) -> None:
        r = _make_recipe(
            RecipeParameter(name="a", type="string", description="", default="x"),
        )
        bound = bind_parameters(r, supplied={})
        assert bound == {"a": "x"}

    def test_unknown_supplied_param_raises(self) -> None:
        r = _make_recipe(
            RecipeParameter(name="a", type="string", description="", required=True),
        )
        with pytest.raises(PreconditionError, match="extra"):
            bind_parameters(r, supplied={"a": "ok", "extra": "nope"})

    def test_number_accepts_int_and_float(self) -> None:
        r = _make_recipe(
            RecipeParameter(name="x", type="number", description="", required=True),
        )
        assert bind_parameters(r, supplied={"x": 1})["x"] == 1
        assert bind_parameters(r, supplied={"x": 1.5})["x"] == 1.5

    def test_boolean_param(self) -> None:
        r = _make_recipe(
            RecipeParameter(name="flag", type="boolean", description="", required=True),
        )
        assert bind_parameters(r, supplied={"flag": True}) == {"flag": True}
        with pytest.raises(PreconditionError):
            bind_parameters(r, supplied={"flag": "yes"})


# ── render_instructions ─────────────────────────────────────────────────────


class TestRenderInstructions:
    def test_single_placeholder(self) -> None:
        r = Recipe(
            name="r",
            description="d",
            version=1,
            instructions="Fix the test at {{test_path}} now.",
            parameters=(),
            tools=RecipeTools(),
            requires=RecipeRequires(),
            source=Path("/tmp/r.yaml"),
        )
        out = render_instructions(r, {"test_path": "tests/unit/test_x.py"})
        assert out == "Fix the test at tests/unit/test_x.py now."

    def test_missing_placeholder_stays_literal(self) -> None:
        r = Recipe(
            name="r",
            description="d",
            version=1,
            instructions="Use {{absent}} and keep it literal.",
            parameters=(),
            tools=RecipeTools(),
            requires=RecipeRequires(),
            source=Path("/tmp/r.yaml"),
        )
        out = render_instructions(r, {})
        assert "{{absent}}" in out

    def test_multiple_placeholders_non_string_values(self) -> None:
        r = Recipe(
            name="r",
            description="d",
            version=1,
            instructions="Run {{path}} {{n}} times; flag={{flag}}.",
            parameters=(),
            tools=RecipeTools(),
            requires=RecipeRequires(),
            source=Path("/tmp/r.yaml"),
        )
        out = render_instructions(r, {"path": "x.py", "n": 10, "flag": True})
        assert out == "Run x.py 10 times; flag=True."


# ── Additional load_recipe validation coverage ───────────────────────────────


class TestLoadRecipeAdditionalValidation:
    def test_version_must_be_int_not_bool(self, tmp_path: Path) -> None:
        """version: true (a bool) must be rejected — bool is a subclass of int."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: true
            instructions: go
            """,
        )
        with pytest.raises(RecipeLoadError, match="version"):
            load_recipe(path)

    def test_parameters_must_be_mapping(self, tmp_path: Path) -> None:
        """parameters as a list must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            parameters:
              - item1
            """,
        )
        with pytest.raises(RecipeLoadError, match="parameters"):
            load_recipe(path)

    def test_parameter_spec_must_be_mapping(self, tmp_path: Path) -> None:
        """A parameter spec given as a string must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            parameters:
              p1: "just a string"
            """,
        )
        with pytest.raises(RecipeLoadError, match="mapping"):
            load_recipe(path)

    def test_parameter_description_must_be_string(self, tmp_path: Path) -> None:
        """A non-string parameter description must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            parameters:
              p1:
                type: string
                description: 42
            """,
        )
        with pytest.raises(RecipeLoadError, match="description"):
            load_recipe(path)

    def test_parameter_required_must_be_bool(self, tmp_path: Path) -> None:
        """A non-bool required flag must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            parameters:
              p1:
                type: string
                description: "a param"
                required: "yes"
            """,
        )
        with pytest.raises(RecipeLoadError, match="boolean"):
            load_recipe(path)

    def test_tools_deny_must_be_list_of_strings(self, tmp_path: Path) -> None:
        """tools.deny as a string must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            tools:
              deny: "not a list"
            """,
        )
        with pytest.raises(RecipeLoadError, match="tools"):
            load_recipe(path)

    def test_tools_must_be_mapping(self, tmp_path: Path) -> None:
        """tools as a list must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            tools:
              - item
            """,
        )
        with pytest.raises(RecipeLoadError, match="tools"):
            load_recipe(path)

    def test_requires_must_be_mapping(self, tmp_path: Path) -> None:
        """requires as a list must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            requires:
              - item
            """,
        )
        with pytest.raises(RecipeLoadError, match="requires"):
            load_recipe(path)

    def test_requires_model_tier_must_be_string(self, tmp_path: Path) -> None:
        """requires.model_tier as an integer must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            requires:
              model_tier: 42
            """,
        )
        with pytest.raises(RecipeLoadError, match="model_tier"):
            load_recipe(path)

    def test_requires_max_turns_must_be_int(self, tmp_path: Path) -> None:
        """requires.max_turns as a float must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            requires:
              max_turns: 3.5
            """,
        )
        with pytest.raises(RecipeLoadError, match="max_turns"):
            load_recipe(path)

    def test_requires_budget_must_not_be_bool(self, tmp_path: Path) -> None:
        """requires.budget_per_story_usd as a bool must be rejected."""
        path = _write_recipe(
            tmp_path,
            """
            name: x
            description: d
            version: 1
            instructions: go
            requires:
              budget_per_story_usd: true
            """,
        )
        with pytest.raises(RecipeLoadError, match="budget"):
            load_recipe(path)


class TestTypeMatches:
    def test_number_rejects_bool(self) -> None:
        """True should NOT match 'number' type."""
        from maxwell_daemon.recipes import _type_matches

        assert _type_matches(True, "number") is False

    def test_unknown_type_returns_false(self) -> None:
        """An undeclared type returns False."""
        from maxwell_daemon.recipes import _type_matches

        assert _type_matches("anything", "uuid") is False  # type: ignore[arg-type]
