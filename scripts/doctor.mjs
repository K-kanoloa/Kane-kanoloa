import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { checkNode, root, pythonFor } from "./environment.mjs";

let failed = false;
function check(label, passed) { console.log(`${passed ? "OK" : "MISSING"} ${label}`); if (!passed) failed = true; }
try { checkNode(); check("Node.js 24 LTS", true); } catch { check("Node.js 24 LTS", false); }
check("JavaScript dependencies", existsSync(path.join(root, "node_modules/next/dist/bin/next")));
const python = pythonFor("apps/api");
check("API Python environment", existsSync(python));
if (existsSync(python)) {
  const imports = spawnSync(python, ["-c", "import fastapi, uvicorn, pydantic, httpx"], { stdio: "ignore", windowsHide: true });
  check("API imports", imports.status === 0);
  const deps = spawnSync(python, ["-m", "pip", "check"], { stdio: "ignore", windowsHide: true });
  check("API dependency compatibility", deps.status === 0);
}
check("Web build", existsSync(path.join(root, "apps/web/.next/BUILD_ID")));
check("Model settings helper", existsSync(path.join(root, "scripts/configure-kanaloa-model.mjs")));
const patch = spawnSync(process.execPath, ["scripts/patch-dsh-acp.mjs"], { cwd: root, stdio: "ignore", windowsHide: true });
check("Bundled Agent runtime", patch.status === 0);
console.log("Configure your model endpoint and key in Kanaloa settings after launch.");
process.exitCode = failed ? 1 : 0;
