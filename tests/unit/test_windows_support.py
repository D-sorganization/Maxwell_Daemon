"""Cross-platform (Windows) support regressions — #981.

Three breakages that prevented the daemon from running on Windows:

1. ``main()`` installed SIGINT/SIGTERM handlers via ``loop.add_signal_handler``
   without a ``NotImplementedError`` guard (crashes on Windows' ProactorEventLoop).
2. ``config/fleet.py`` resolved the home dir via ``os.environ["HOME"]`` (unset on
   Windows) so ``~/.maxwell-daemon/fleet.yaml`` was silently never found.
3. ``workspace_hooks._run_hook`` split commands with POSIX-mode shlex, mangling
   Windows paths like ``C:\\tools\\x.exe``.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import pytest

from maxwell_daemon.config import fleet as fleet_mod
from maxwell_daemon.daemon import workspace_hooks

# ── (1) main() signal-handler guard ──────────────────────────────────────────


class TestSignalHandlerGuard:
    def test_main_starts_when_add_signal_handler_unsupported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A loop that raises NotImplementedError (Windows) must not crash main()."""
        from maxwell_daemon.daemon import runner as runner_mod

        signals_set: list[int] = []

        class _FakeLoop:
            def add_signal_handler(self, sig: int, cb: object) -> None:
                raise NotImplementedError("Windows ProactorEventLoop")

            def call_soon_threadsafe(self, cb: object, *a: object) -> None:
                cb()

        # Drive the daemon lifecycle with stubs so we exercise only the signal
        # wiring in main()'s _run(), not a real daemon.
        class _FakeDaemon:
            _config = type("C", (), {"log_file": None})()

            @classmethod
            def from_config_path(cls) -> _FakeDaemon:
                return cls()

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

            def reload_config(self) -> None:
                return None

        def _fake_signal(sig: int, handler: object) -> None:
            signals_set.append(sig)

        import maxwell_daemon.logging as logging_mod

        monkeypatch.setattr(runner_mod.Daemon, "from_config_path", _FakeDaemon.from_config_path)
        monkeypatch.setattr(runner_mod.asyncio, "get_event_loop", lambda: _FakeLoop())
        monkeypatch.setattr(runner_mod.signal, "signal", _fake_signal)
        monkeypatch.setattr(logging_mod, "configure_logging", lambda **kw: None)

        # Make stop.wait() return immediately so _run() completes.
        real_event = asyncio.Event

        class _ImmediateEvent(real_event):  # type: ignore[misc,valid-type]
            async def wait(self) -> bool:
                return True

        monkeypatch.setattr(runner_mod.asyncio, "Event", _ImmediateEvent)

        # Should not raise despite add_signal_handler being unsupported.
        runner_mod.main()
        # Fallback path registered SIGINT/SIGTERM via signal.signal.
        assert runner_mod.signal.SIGINT in signals_set
        assert runner_mod.signal.SIGTERM in signals_set


# ── (2) fleet.py home resolution ─────────────────────────────────────────────


class TestFleetHomeResolution:
    def test_home_candidate_uses_path_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The ~/.maxwell-daemon/fleet.yaml candidate is derived from Path.home()."""
        # HOME unset (as on Windows) must not drop the home candidate.
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(fleet_mod.Path, "home", classmethod(lambda cls: tmp_path))

        candidates = fleet_mod._candidate_paths()

        expected = tmp_path / fleet_mod._DEFAULT_HOME_SUBPATH
        assert expected in candidates

    def test_unresolvable_home_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A RuntimeError from Path.home() degrades gracefully (no crash)."""

        def _boom(cls: object) -> Path:
            raise RuntimeError("no home")

        monkeypatch.setattr(fleet_mod.Path, "home", classmethod(_boom))
        # cwd candidate still present; the ~/.maxwell-daemon/fleet.yaml home
        # candidate is simply omitted rather than raising.
        candidates = fleet_mod._candidate_paths()
        home_subpath = str(fleet_mod._DEFAULT_HOME_SUBPATH)
        assert all(home_subpath not in str(p) for p in candidates)


# ── (3) workspace_hooks Windows path splitting ───────────────────────────────


class TestHookCommandSplitting:
    def test_windows_path_survives_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On Windows (os.name == 'nt') a backslash path is not mangled."""
        monkeypatch.setattr(workspace_hooks.os, "name", "nt")
        # Mirror the production split to assert the policy directly.
        args = shlex.split(r"C:\tools\x.exe --flag", posix=(workspace_hooks.os.name != "nt"))
        assert args[0] == r"C:\tools\x.exe"

    def test_posix_split_still_used_off_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On POSIX, normal quoting semantics are preserved."""
        monkeypatch.setattr(workspace_hooks.os, "name", "posix")
        args = shlex.split("echo 'a b'", posix=(workspace_hooks.os.name != "nt"))
        assert args == ["echo", "a b"]

    def test_run_hook_parses_windows_path_without_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_hook on Windows splits a native path into a single argv[0]."""
        monkeypatch.setattr(workspace_hooks.os, "name", "nt")
        captured: dict[str, object] = {}

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def fake_create(*args: object, **kwargs: object) -> _Proc:
            captured["args"] = args
            return _Proc()

        monkeypatch.setattr(workspace_hooks.asyncio, "create_subprocess_exec", fake_create)

        async def _run() -> None:
            await workspace_hooks._run_hook(
                "win", r"C:\tools\x.exe --flag", tmp_path, timeout_seconds=5
            )

        asyncio.run(_run())
        assert captured["args"][0] == r"C:\tools\x.exe"  # type: ignore[index]
