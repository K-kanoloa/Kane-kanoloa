import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export function checkNode() {
  if (Number(process.versions.node.split(".")[0]) !== 24) throw new Error("Install Node.js 24 LTS before continuing.");
}
export function loadSettings() {
  const filename = path.join(root, ".env");
  if (existsSync(filename)) process.loadEnvFile(filename);
}
export function run(command, args, cwd = root) {
  const result = spawnSync(command, args, { cwd, stdio: "inherit", shell: false, windowsHide: true });
  if (result.error || result.status !== 0) throw new Error(`Command failed (${path.basename(command)}). See the output above.`);
}
export function pythonFor(app) {
  return path.join(root, app, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
}
export function findPython() {
  const candidates = process.env.PYTHON ? [[process.env.PYTHON, []]] : process.platform === "win32"
    ? [["py", ["-3"]], ["python", []], ["python3", []]] : [["python3", []], ["python", []]];
  for (const [command, prefix] of candidates) {
    const result = spawnSync(command, [...prefix, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"], { stdio: "ignore", windowsHide: true });
    if (!result.error && result.status === 0) return { command, prefix };
  }
  throw new Error("Python 3.11+ with venv is required. Install it, or set PYTHON to its executable path.");
}
export function npm(args) {
  if (!process.env.npm_execpath) throw new Error("Run this command through npm.");
  run(process.execPath, [process.env.npm_execpath, ...args]);
}
