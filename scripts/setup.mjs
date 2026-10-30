import { checkNode, loadSettings, findPython, npm, run } from "./environment.mjs";

try {
  checkNode(); loadSettings(); findPython();
  npm(["ci"]);
  run(process.execPath, ["scripts/setup-python.mjs", "apps/api"]);
  if (process.argv.includes("--with-bridge")) run(process.execPath, ["scripts/setup-python.mjs", "apps/local-bridge"]);
  npm(["run", "build"]);
  console.log("Installation complete. Run npm start, then open http://127.0.0.1:3000.");
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
