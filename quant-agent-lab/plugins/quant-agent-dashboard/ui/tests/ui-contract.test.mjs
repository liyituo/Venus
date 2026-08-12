import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const app = await readFile(resolve(root, "src", "app.js"), "utf8");
const css = await readFile(resolve(root, "src", "style.css"), "utf8");
const html = await readFile(resolve(root, "src", "index.html"), "utf8");

test("UI uses shared MCP Apps bridge and keeps window.openai optional", () => {
  for (const method of ["ui/initialize", "ui/notifications/tool-input", "ui/notifications/tool-result", "tools/call", "ui/message"]) {
    assert.match(app, new RegExp(method.replaceAll("/", "\\/")));
  }
  assert.match(app, /window\.openai && typeof window\.openai\.callTool/);
  assert.doesNotMatch(app, /innerHTML/);
  assert.match(app, /quant_execute_paper_plan/);
  assert.match(app, /quant_get_chart_data/);
  assert.match(app, /quant_run_strategy_debug/);
  assert.match(app, /quant_run_backtest/);
  assert.match(app, /SANDBOX_UNAVAILABLE/);
  assert.match(app, /market-svg/);
  assert.match(app, /PAPER TRADING/);
  assert.doesNotMatch(app, /eval\s*\(/);
  assert.doesNotMatch(app, /exec\s*\(/);
});

test("UI has security and responsive presentation contracts", () => {
  assert.match(html, /Content-Security-Policy/);
  assert.match(html, /frame-ancestors \*;/);
  assert.match(css, /min-width: 320px/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /max-width: 360px/);
  assert.match(css, /\.risk-status/);
});
