import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pluginRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillRoot = process.env.CODEX_PLUGIN_CREATOR_ROOT;
if (!skillRoot) {
  console.error("Set CODEX_PLUGIN_CREATOR_ROOT to the local plugin-creator skill directory.");
  process.exit(2);
}
const script = resolve(skillRoot, "scripts", "validate_plugin.py");
const result = spawnSync(process.env.PYTHON ?? "python", [script, pluginRoot], {
  stdio: "inherit",
});
process.exit(result.status ?? 1);
