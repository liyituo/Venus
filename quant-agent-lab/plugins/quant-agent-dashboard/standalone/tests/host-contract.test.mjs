import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = await readFile(resolve(root, "server.mjs"), "utf8");

test("standalone host is loopback-only and delegates the real MCP bridge", () => {
  assert.match(source, /server\.listen\(port, "127\.0\.0\.1"/);
  assert.match(source, /QUANT_AGENT_STANDALONE_PORT \?\? 4173/);
  assert.match(source, /url\.pathname === "\/healthz"/);
  assert.match(source, /url\.pathname === "\/api\/connection"/);
  assert.match(source, /url\.pathname === "\/bridge" && request\.method === "POST"/);
  assert.match(source, /body\.name\.startsWith\("quant_"\)/);
  assert.match(source, /callTool\(body\.name, body\.arguments \?\? \{\}, backend\)/);
  assert.doesNotMatch(source, /harness|scenario/i);
  assert.doesNotMatch(source, /quant_generate_daily_plan|quant_submit_approval|quant_execute_paper_plan/);
});
