import { existsSync } from "node:fs";
import path from "node:path";
import { root, loadSettings, findPython, pythonFor, run } from "./environment.mjs";

try {
  loadSettings();
  const app = process.argv[2];
  if (!["apps/api", "apps/local-bridge", "connectors/codex", "connectors/kane-mcp"].includes(app)) throw new Error("Unknown application directory.");
  const directory = path.join(root, app);
  const python = pythonFor(app);
  if (!existsSync(python)) {
    const selected = findPython();
    run(selected.command, [...selected.prefix, "-m", "venv", ".venv"], directory);
  }
  run(python, ["-m", "pip", "install", "--require-hashes", "-r", "requirements.txt"], directory);
  run(python, ["-m", "pip", "check"], directory);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
