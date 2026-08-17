"use strict";

const { spawn } = require("child_process");
const { performance } = require("perf_hooks");
const path = require("path");

const budgetMs = Number(process.env.MAXWELL_DESKTOP_LAUNCH_BUDGET_MS || 2000);
const timeoutMs = Math.max(5000, budgetMs * 3);
const launchStartedAt = performance.now();
const electronBinary = require("electron");

const extraArgs = (process.env.ELECTRON_EXTRA_LAUNCH_ARGS || "")
  .split(/\s+/)
  .map((s) => s.trim())
  .filter(Boolean);

if (process.env.ELECTRON_DISABLE_SANDBOX === "1" && !extraArgs.includes("--no-sandbox")) {
  extraArgs.unshift("--no-sandbox");
}

const child = spawn(electronBinary, [...extraArgs, __dirname], {
  cwd: __dirname,
  env: {
    ...process.env,
    MAXWELL_DESKTOP_LAUNCH_BUDGET_MS: String(budgetMs),
    MAXWELL_DESKTOP_LAUNCH_SMOKE: "1",
  },
  stdio: ["ignore", "pipe", "pipe"],
});

let stdout = "";
let stderr = "";
let settled = false;

function finish(code) {
  if (settled) return;
  settled = true;
  clearTimeout(timer);
  try {
    child.kill();
  } catch (_) {}
  const wallElapsedMs = Math.round(performance.now() - launchStartedAt);
  const resultLine = stdout.trim().split(/\r?\n/).find((line) => line.startsWith("{"));
  if (!resultLine) {
    console.error(stderr.trim() || "desktop launch smoke did not emit a timing result");
    process.exit(1);
  }
  const result = JSON.parse(resultLine);
  const passed = result.passed && wallElapsedMs <= budgetMs;
  console.log(`desktop ready in ${wallElapsedMs}ms (app ${result.elapsedMs}ms, budget ${result.budgetMs}ms)`);
  process.exit(code || (passed ? 0 : 1));
}

const timer = setTimeout(() => {
  if (settled) return;
  settled = true;
  try {
    child.kill();
  } catch (_) {}
  console.error(`desktop launch smoke timed out after ${timeoutMs}ms`);
  process.exit(1);
}, timeoutMs);

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
