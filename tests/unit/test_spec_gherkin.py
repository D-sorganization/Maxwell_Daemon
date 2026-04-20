"""Tests for spec ingestion — Gherkin (.feature) loader and pytest-bdd scaffold.

The spec-driven flow: user writes a ``.feature`` file; the spec loader
parses it; the scaffold generator emits a pytest-bdd skeleton; the agent
fills in step definitions and the implementation; the gate refuses to
close the task until every scenario passes.

These tests cover the loader and scaffold generator. Execution
enforcement (agent loop integration) lives in a separate test file.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from maxwell_daemon.spec import (
    GherkinParseError,
    Scenario,
    Specification,
    load_feature,
    load_spec_directory,
    render_pytest_bdd_scaffold,
)


def _write_feature(tmp_path: Path, body: str, name: str = "login.feature") -> Path:
    path = tmp_path / name
    path.write_text(dedent(body).lstrip())
    return path


# ── Shape ────────────────────────────────────────────────────────────────────


class TestShapes:
    def test_specification_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        spec = Specification(
            feature="Login",
            description="",
            source=Path("/tmp/x"),
            scenarios=(),
            tags=(),
        )
        with pytest.raises(FrozenInstanceError):
            spec.feature = "other"  # type: ignore[misc]

    def test_scenario_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        s = Scenario(name="x", steps=(), tags=())
        with pytest.raises(FrozenInstanceError):
            s.name = "y"  # type: ignore[misc]


# ── Gherkin parsing ──────────────────────────────────────────────────────────


class TestGherkinParsing:
    def test_happy_path_feature(self, tmp_path: Path) -> None:
        path = _write_feature(
            tmp_path,
            """
            Feature: Login
              Users log in with email and password.

              Scenario: Successful login
                Given a user with email "alice@example.com"
                When they log in with the correct password
                Then the dashboard is displayed
            """,
        )
        spec = load_feature(path)
        assert spec.feature == "Login"
        assert "Users log in" in spec.description
        assert len(spec.scenarios) == 1
        scenario = spec.scenarios[0]
        assert scenario.name == "Successful login"
        assert len(scenario.steps) == 3
        assert scenario.steps[0].keyword == "Given"
        assert "alice@example.com" in scenario.steps[0].text

    def test_multiple_scenarios(self, tmp_path: Path) -> None:
        path = _write_feature(
            tmp_path,
            """
            Feature: Auth

              Scenario: Successful login
                Given a user
                When they log in
                Then they see the dashboard

              Scenario: Wrong password
                Given a user
                When they submit a bad password
                Then they see the error banner
            """,
        )
        spec = load_feature(path)
        assert len(spec.scenarios) == 2
        assert [s.name for s in spec.scenarios] == [
            "Successful login",
            "Wrong password",
        ]

    def test_and_but_continuations(self, tmp_path: Path) -> None:
        path = _write_feature(
            tmp_path,
            """
            Feature: Cart

              Scenario: Checkout
                Given a logged-in user
                And a cart with 3 items
                When they click Checkout
                And they enter a valid card
                Then the order is confirmed
                But no email is sent yet
            """,
        )
        spec = load_feature(path)
        keywords = [s.keyword for s in spec.scenarios[0].steps]
        assert keywords == ["Given", "And", "When", "And", "Then", "But"]

    def test_tags_captured(self, tmp_path: Path) -> None:
        path = _write_feature(
            tmp_path,
            """
            @auth @wip
            Feature: Login

              @happy
              Scenario: Successful login
                Given a user
                When they log in
                Then they see the dashboard
            """,
        )
        spec = load_feature(path)
        assert set(spec.tags) == {"@auth", "@wip"}
        assert set(spec.scenarios[0].tags) == {"@happy"}

    def test_feature_missing_rejected(self, tmp_path: Path) -> None:
        path = _write_feature(tmp_path, "just some text\nwith no feature block\n")
        with pytest.raises(GherkinParseError, match="Feature"):
            load_feature(path)

    def test_scenario_without_steps_rejected(self, tmp_path: Path) -> None:
        path = _write_feature(
            tmp_path,
            """
            Feature: Empty

              Scenario: Nothing
            """,
        )
        with pytest.raises(GherkinParseError, match="step"):
            load_feature(path)

    def test_comments_ignored(self, tmp_path: Path) -> None:
        path = _write_feature(
            tmp_path,
            """
            Feature: Login
              # comment inline
              Scenario: A
                Given x
                # another comment
                When y
                Then z
            """,
        )
        spec = load_feature(path)
        assert len(spec.scenarios) == 1
        assert len(spec.scenarios[0].steps) == 3


# ── Directory loader ────────────────────────────────────────────────────────


class TestLoadSpecDirectory:
    def test_returns_empty_for_missing_dir(self, tmp_path: Path) -> None:
        assert load_spec_directory(tmp_path / "nope") == ()

    def test_loads_every_feature_file(self, tmp_path: Path) -> None:
        _write_feature(
            tmp_path,
            "Feature: A\n  Scenario: a1\n    Given x\n    When y\n    Then z\n",
            name="a.feature",
        )
        _write_feature(
            tmp_path,
            "Feature: B\n  Scenario: b1\n    Given x\n    When y\n    Then z\n",
            name="b.feature",
        )
        specs = load_spec_directory(tmp_path)
        assert sorted(s.feature for s in specs) == ["A", "B"]

    def test_ignores_non_feature_files(self, tmp_path: Path) -> None:
        (tmp_path / "readme.md").write_text("not a feature")
        _write_feature(
            tmp_path,
            "Feature: A\n  Scenario: a1\n    Given x\n    When y\n    Then z\n",
            name="a.feature",
        )
        specs = load_spec_directory(tmp_path)
        assert [s.feature for s in specs] == ["A"]

    def test_one_malformed_file_does_not_kill_loader(self, tmp_path: Path) -> None:
        _write_feature(tmp_path, "broken nonsense\n", name="bad.feature")
        _write_feature(
            tmp_path,
            "Feature: A\n  Scenario: a1\n    Given x\n    When y\n    Then z\n",
            name="a.feature",
        )
        specs = load_spec_directory(tmp_path)
        # Broken file is skipped; the valid one still loads.
        assert [s.feature for s in specs] == ["A"]


# ── pytest-bdd scaffold ─────────────────────────────────────────────────────


class TestPytestBddScaffold:
    def test_scaffold_defines_scenarios_binding(self) -> None:
        spec = Specification(
            feature="Login",
            description="",
            source=Path("specs/login.feature"),
            scenarios=(
                Scenario(
                    name="Successful login",
                    steps=(),
                    tags=(),
                ),
            ),
            tags=(),
        )
        code = render_pytest_bdd_scaffold(spec)
        assert "from pytest_bdd import" in code
        assert 'scenarios("specs/login.feature")' in code

    def test_scaffold_includes_step_stubs(self) -> None:
        from maxwell_daemon.spec import Step

        spec = Specification(
            feature="Login",
            description="",
            source=Path("specs/login.feature"),
            scenarios=(
                Scenario(
                    name="Successful login",
                    steps=(
                        Step(keyword="Given", text='a user with email "alice"'),
                        Step(keyword="When", text="they log in"),
                        Step(keyword="Then", text="they see the dashboard"),
                    ),
                    tags=(),
                ),
            ),
            tags=(),
        )
        code = render_pytest_bdd_scaffold(spec)
        assert "@given(" in code
        assert "@when(" in code
        assert "@then(" in code
        assert "a user with email" in code

    def test_scaffold_dedupes_repeated_steps(self) -> None:
        from maxwell_daemon.spec import Step

        spec = Specification(
            feature="Search",
            description="",
            source=Path("specs/search.feature"),
            scenarios=(
                Scenario(
                    name="A",
                    steps=(
                        Step(keyword="Given", text="a user"),
                        Step(keyword="When", text="they search"),
                        Step(keyword="Then", text="they see results"),
                    ),
                    tags=(),
                ),
                Scenario(
                    name="B",
                    steps=(
                        Step(keyword="Given", text="a user"),
                        Step(keyword="When", text="they search"),
                        Step(keyword="Then", text="they see results"),
                    ),
                    tags=(),
                ),
            ),
            tags=(),
        )
        code = render_pytest_bdd_scaffold(spec)
        # Each step text appears exactly once as a @given/@when/@then stub.
        assert code.count('@given("a user")') == 1
        assert code.count('@when("they search")') == 1
        assert code.count('@then("they see results")') == 1
