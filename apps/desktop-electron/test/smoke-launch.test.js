"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { evaluateSmokeResult, resolveBinaryThenStartClock } = require("../smoke-launch");

test("launch clock starts only after the Electron binary is resolved", () => {
  let clock = 1000;
  const now = () => clock;
  const resolveBinary = () => {
    // Simulates the lazy "Downloading Electron binary..." in require("electron").
    clock += 233000;
    return "/fake/electron";
  };

  const { binary, startedAt } = resolveBinaryThenStartClock(resolveBinary, now);
  clock += 119;

  assert.equal(binary, "/fake/electron");
  assert.equal(startedAt, 234000);
  assert.equal(Math.round(now() - startedAt), 119);
});

test("smoke passes when app reports ready within budget", () => {
  const stdout = 'noise\n{"passed":true,"elapsedMs":119,"budgetMs":180000}\n';
  const outcome = evaluateSmokeResult(stdout, 450, 180000);

  assert.equal(outcome.passed, true);
  assert.equal(outcome.summary, "desktop ready in 450ms (app 119ms, budget 180000ms)");
});

test("smoke fails when launch wall time exceeds budget", () => {
  const stdout = '{"passed":true,"elapsedMs":119,"budgetMs":2000}\n';

  assert.equal(evaluateSmokeResult(stdout, 2001, 2000).passed, false);
});

test("smoke fails without a timing result line", () => {
  assert.equal(evaluateSmokeResult("no json here\n", 10, 2000), null);
});
