import asyncio

import pytest

from maxwell_daemon.core.execution_sandbox import ExecutionSandbox


@pytest.mark.asyncio
async def test_execution_sandbox_cleanup_flags():
    # We test the interface and guarantee that the --rm flag is present
    sandbox = ExecutionSandbox()

    # We won't actually run docker in unit tests if it's not installed,
    # but we can mock the subprocess call to verify the contract.
    class MockProcess:
        returncode = 0

        async def communicate(self):
            return b"hello from sandbox", b""

    # Intercept create_subprocess_exec
    original_exec = asyncio.create_subprocess_exec
    cmd_run = []

    async def mock_exec(*args, **kwargs):
        cmd_run.extend(args)
        return MockProcess()

    asyncio.create_subprocess_exec = mock_exec
    try:
        result = await sandbox.run_command("echo hello")

        assert result.exit_code == 0
        assert result.stdout == "hello from sandbox"
        assert "docker" in cmd_run
        assert "--rm" in cmd_run  # Strict verification of disk space cleanup
        assert "--network" in cmd_run
        assert "none" in cmd_run  # Strict verification of isolation
    finally:
        asyncio.create_subprocess_exec = original_exec
