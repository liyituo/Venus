import { createInterface } from "node:readline";
import { BackendClient } from "./backend-client.mjs";
import { handleRequest } from "./protocol.mjs";

const backend = new BackendClient();
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });

for await (const line of input) {
  if (!line.trim()) continue;
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } })}\n`);
    continue;
  }
  try {
    const response = await handleRequest(request, { backend });
    if (response) process.stdout.write(`${JSON.stringify(response)}\n`);
  } catch (error) {
    process.stderr.write(`${error.stack ?? error}\n`);
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id ?? null, error: { code: -32603, message: "internal error" } })}\n`);
  }
}
