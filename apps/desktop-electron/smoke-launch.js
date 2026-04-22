"use strict";

const { spawn } = require("child_process");
const { performance } = require("perf_hooks");
const path = require("path");

const budgetMs = Number(process.env.MAXWELL_DESKTOP_LAUNCH_BUDGET_MS || 2000);
const timeoutMs = Math.max(5000, budgetMs * 3);
const launchStartedAt = performance.now();
const executable = process.platform === "win32"
  ? path.join(__dirname, "node_modules", ".bin", "electron.cmd")
  : path.join(__dirname, "node_modules", ".bin", "electron");

const child = spawn(executable, [__dirname], {
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

const timer = setTimeout(() => {
  if (settled) return;
  settled = true;
  child.kill();
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
  if (settled) return;
  settled = true;
  clearTimeout(timer);
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
});
