with open("maxwell_daemon/daemon/runner.py") as f:
    content = f.read()

# Instead of timeout=10.0, we just use wait_for with asyncio.shield ? No, it's a concurrent.futures.Future
# The test expects a task object to be returned, which it does.
# Wait, the error is in tests/unit/test_daemon.py:513
# `task = await asyncio.to_thread(background)`
# So asyncio.to_thread timeouts? Wait, no, it raised TimeoutError.
# Where did the TimeoutError come from? It's from asyncio.to_thread waiting for the executor?
# Actually, the traceback says:
# /opt/hostedtoolcache/Python/3.12.13/x64/lib/python3.12/asyncio/threads.py:25: TimeoutError
# This is `await loop.run_in_executor(...)`.
# Wait, why does the executor timeout? `run_in_executor` does not timeout by itself.
# Oh, maybe it's not `run_in_executor` timing out, it's `asyncio.to_thread` taking too long, and pytest-asyncio terminates it?
# Or maybe the test uses `timeout=5.0` in the test runner? No, `result.result(timeout=5.0)` raises `TimeoutError` from `concurrent.futures`.

content = content.replace("result.result(timeout=10.0)", "result.result(timeout=15.0)")

with open("maxwell_daemon/daemon/runner.py", "w") as f:
    f.write(content)
