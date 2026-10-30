import { existsSync } from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:net";
import path from "node:path";
import { checkNode, loadSettings, root, pythonFor } from "./environment.mjs";

const children = [];
let stopping = false;
function port(name, fallback) {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isInteger(value) || value < 1 || value > 65535) throw new Error(`Invalid ${name}.`);
  return value;
}
async function available(value) {
  await new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", () => reject(new Error(`Port ${value} is already in use. Stop the other service or choose another port in .env.`)));
    server.listen(value, "127.0.0.1", () => server.close(resolve));
  });
}
function signal(child, force = false) {
  if (!child.pid) return;
  if (process.platform === "win32") {
    if (child.exitCode === null && child.signalCode === null) spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
  } else {
    try { process.kill(-child.pid, force ? "SIGKILL" : "SIGTERM"); }
    catch (error) { if (error.code !== "ESRCH") console.error("Could not stop an owned service."); }
  }
}
async function shutdown(code) {
  if (stopping) return;
  stopping = true;
  for (const { child } of children.toReversed()) signal(child);
  let timer;
  await Promise.race([Promise.all(children.map(item => item.done)), new Promise(resolve => { timer = setTimeout(resolve, 10000); })]);
  clearTimeout(timer);
  for (const { child } of children) signal(child, true);
  process.exitCode = code;
}
function start(name, command, args, cwd, env) {
  const child = spawn(command, args, { cwd, env, stdio: "inherit", shell: false, windowsHide: true, detached: process.platform !== "win32" });
  const done = new Promise(resolve => {
    child.once("exit", (code, reason) => {
      resolve();
      if (!stopping) { console.error(`${name} stopped unexpectedly (${reason ?? code}).`); void shutdown(code || 1); }
    });
    child.once("error", () => { resolve(); console.error(`${name} could not start.`); void shutdown(1); });
  });
  children.push({ child, done });
}
async function ready(url) {
  const end = Date.now() + 90000;
  while (!stopping && Date.now() < end) {
    try { if ((await fetch(url, { signal: AbortSignal.timeout(1500) })).ok) return; } catch { /* Service may still be starting. */ }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (!stopping) throw new Error("Startup timed out. Check the service output above.");
}
try {
  checkNode(); loadSettings();
  const apiPort = port("KANE_API_PORT", 8000), webPort = port("KANE_WEB_PORT", 3000);
  const bridge = process.argv.includes("--with-bridge");
  const bridgePort = bridge ? port("KANE_BRIDGE_PORT", 8010) : null;
  const ports = [apiPort, webPort, ...(bridge ? [bridgePort] : [])];
  if (new Set(ports).size !== ports.length) throw new Error("Each service needs a different port.");
  for (const value of ports) await available(value);
  for (const app of ["apps/api", ...(bridge ? ["apps/local-bridge"] : [])]) {
    if (!existsSync(pythonFor(app))) throw new Error(`Missing Python environment for ${app}. Run npm run ${app.endsWith("api") ? "setup" : "setup:bridge"} first.`);
  }
  const next = path.join(root, "node_modules/next/dist/bin/next");
  if (!existsSync(next) || !existsSync(path.join(root, "apps/web/.next/BUILD_ID"))) throw new Error("Missing web build. Run npm run setup first.");
  const env = { ...process.env, NEXT_TELEMETRY_DISABLED: "1", KANE_API_BASE_URL: `http://127.0.0.1:${apiPort}` };
  if (env.KANALOA_TOOLS_ENABLED === undefined) {
    const image = spawnSync("docker", ["image", "inspect", "kane-kanaloa-tools:0.1.0"], { stdio: "ignore", timeout: 5000, windowsHide: true });
    env.KANALOA_TOOLS_ENABLED = image.status === 0 ? "1" : "0";
    if (env.KANALOA_TOOLS_ENABLED === "0") console.log("Optional Python tools are off. Enable them after npm run setup:kanaloa-tools.");
  }
  process.once("SIGINT", () => void shutdown(0));
  process.once("SIGTERM", () => void shutdown(0));
  start("API", pythonFor("apps/api"), ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(apiPort), "--timeout-graceful-shutdown", "5"], path.join(root, "apps/api"), env);
  start("Web", process.execPath, [next, "start", "--hostname", "127.0.0.1", "--port", String(webPort)], path.join(root, "apps/web"), env);
  if (bridge) start("Local Bridge", pythonFor("apps/local-bridge"), ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(bridgePort), "--timeout-graceful-shutdown", "5"], path.join(root, "apps/local-bridge"), env);
  await Promise.all([ready(`http://127.0.0.1:${apiPort}/health`), ready(`http://127.0.0.1:${webPort}`)]);
  if (!stopping) console.log(`Kane-Kanaloa is ready: http://127.0.0.1:${webPort}\nPress Ctrl+C to stop.`);
} catch (error) {
  console.error(error.message);
  await shutdown(1);
}
