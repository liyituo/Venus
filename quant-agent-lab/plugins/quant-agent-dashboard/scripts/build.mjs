import { cp, mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const uiSource = resolve(root, "ui", "src");
const uiPublic = resolve(root, "ui", "public");
const uiDist = resolve(root, "ui", "dist");
const mcpSource = resolve(root, "mcp-server", "src");
const mcpDist = resolve(root, "mcp-server", "dist");

await rm(uiDist, { recursive: true, force: true });
await rm(mcpDist, { recursive: true, force: true });
await mkdir(uiDist, { recursive: true });
await mkdir(mcpDist, { recursive: true });
await cp(uiSource, uiDist, { recursive: true });
await cp(uiPublic, uiDist, { recursive: true, force: true });
await cp(mcpSource, mcpDist, { recursive: true });
console.log(`Built UI: ${uiDist}`);
console.log(`Built MCP server: ${mcpDist}`);
