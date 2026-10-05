"use strict";

const { spawn } = require("child_process");
const os = require("os");
const { performance } = require("perf_hooks");
const path = require("path");

/**
 * Resolve the Electron executable, then start the launch clock.
 *
 * `require("electron")` downloads the binary on first use ("Downloading
 * Electron binary..."). That download is provisioning, not launch latency, so
 * the budget clock must start only once the binary path is known.
 */
function resolveBinaryThenStartClock(resolveBinary, now) {
  const binary = resolveBinary();
  return { binary, startedAt: now() };
}

/** Parse the app's JSON timing line and apply the wall-clock launch budget. */
function evaluateSmokeResult(stdout, wallElapsedMs, budgetMs) {
  const resultLine = stdout.trim().split(/\r?\n/).find((line) => line.startsWith("{"));
  if (!resultLine) return null;
  const result = JSON.parse(resultLine);
  return {
    passed: Boolean(result.passed) && wallElapsedMs <= budgetMs,
    summary: `desktop ready in ${wallElapsedMs}ms (app ${result.elapsedMs}ms, budget ${result.budgetMs}ms)`,
  };
}

function launchArgs() {
  const extraArgs = (process.env.ELECTRON_EXTRA_LAUNCH_ARGS || "")
    .split(/\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (process.env.ELECTRON_DISABLE_SANDBOX === "1" && !extraArgs.includes("--no-sandbox")) {
    extraArgs.unshift("--no-sandbox");
  }
  return [...extraArgs, __dirname];
}

function main() {
  const budgetMs = Number(process.env.MAXWELL_DESKTOP_LAUNCH_BUDGET_MS || 2000);
  const timeoutMs = Math.max(5000, budgetMs * 3);
  const { binary, startedAt } = resolveBinaryThenStartClock(
    () => require("electron"),
    () => performance.now(),
  );

  const child = spawn(binary, launchArgs(), {
    cwd: __dirname,
    env: {
      ...process.env,
      MAXWELL_DESKTOP_LAUNCH_BUDGET_MS: String(budgetMs),
      MAXWELL_DESKTOP_LAUNCH_SMOKE: "1",
      XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME || path.join(os.tmpdir(), "maxwell-electron-config"),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stdout = "";
  let stderr = "";
  let settled = false;

  const timer = setTimeout(() => {
    if (settled) return;
    settled = true;
    try {
      child.kill();
    } catch (_) {}
    console.error(`desktop launch smoke timed out after ${timeoutMs}ms`);
    process.exit(1);
  }, timeoutMs);

  function finish(code) {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    try {
      child.kill();
    } catch (_) {}
    const wallElapsedMs = Math.round(performance.now() - startedAt);
    const outcome = evaluateSmokeResult(stdout, wallElapsedMs, budgetMs);
    if (!outcome) {
      console.error(stderr.trim() || "desktop launch smoke did not emit a timing result");
      process.exit(1);
    }
    console.log(outcome.summary);
    process.exit(code || (outcome.passed ? 0 : 1));
  }

  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
  });

  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  child.on("error", (error) => {
    clearTimeout(timer);
    console.error(error.message);
    process.exit(1);
  });

  child.on("exit", (code) => {
    finish(code);
  });
}

module.exports = { evaluateSmokeResult, resolveBinaryThenStartClock };

if (require.main === module) {
  main();
}
