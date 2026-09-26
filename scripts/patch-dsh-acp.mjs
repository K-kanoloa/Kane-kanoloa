#!/usr/bin/env node
/**
 * scripts/patch-dsh-acp.mjs
 * 
 * Formal, reproducible build/setup script for Kanaloa's downstream DSH Steer Extension.
 * Applies the verified Native Steer extension to project-local @deepseek-ai/dsh-acp:
 * 1. AcpSession.prototype.steer(params) -> native agent.steer(message)
 * 2. connection.onRequest("session/steer", ...) and onNotification("session/steer", ...)
 * 
 * Strict Fail-Closed Design:
 * - Targets ONLY project-local node_modules/@deepseek-ai/dsh-acp
 * - Asserts @deepseek-ai/dsh-acp package.json exists and version === "0.1.5-rc.3"
 * - Asserts exact upstream code anchors exist before patching
 * - Never scans or modifies machine-level / global npm cache
 * - Idempotent: safe to run multiple times, on postinstall or setup:dsh
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, '..');

const EXPECTED_VERSION = '0.1.5-rc.3';

const STEER_METHOD_CODE = `	/** Submit steering for the nearest step boundary without cancelling or restarting. */
	async steer(params) {
		this.assertActive();
		if (this.ctx.agents.get(this.agent.id) !== this.agent) {
			throw internalError$1("steer was not queued: the agent was disposed");
		}
		if (this.agent.status !== "running") {
			throw invalidParams$1(\`cannot steer session "\${this.agent.session.id}": session is not running (status: \${this.agent.status})\`);
		}
		const promptBlocks = Array.isArray(params.prompt) ? params.prompt : [{ type: "text", text: String(params.prompt ?? "") }];
		const promptSelection = this.modelControl.snapshot();
		const admissionController = new AbortController();
		const content = await admitAcpPrompt(this.ctx, promptSelection, promptBlocks, false, admissionController.signal);
		admissionController.signal.throwIfAborted();
		const message = createUserMessage({
			content,
			source: { kind: "user" }
		});
		this.agent.steer(message);
		return { accepted: true };
	}`;

const IMPLEMENTATION_STEER_CODE = `		async steer(params) {
			assertOpen();
			return requireSession(brandString(params.sessionId)).steer(params);
		},`;

const CONNECTION_STEER_CODE = `.onRequest("session/steer", (params) => params, ({ params }) => implementation.steer(params)).onNotification("session/steer", (params) => params, ({ params }) => implementation.steer(params))`;

function countOccurrences(content, needle) {
  let count = 0;
  let pos = 0;
  while ((pos = content.indexOf(needle, pos)) !== -1) {
    count++;
    pos += needle.length;
  }
  return count;
}

function verifyAndPatchProjectDshAcp() {
  const targetDir = path.join(repoRoot, 'node_modules', '@deepseek-ai', 'dsh-acp');
  const pkgJsonPath = path.join(targetDir, 'package.json');
  const indexPath = path.join(targetDir, 'lib', 'index.js');

  if (!fs.existsSync(targetDir) || !fs.existsSync(pkgJsonPath)) {
    throw new Error(
      `[patch-dsh-acp] FATAL: @deepseek-ai/dsh-acp not found at "${targetDir}". Run "npm install" first.`
    );
  }

  // 1. Strict version assertion (fail closed)
  const pkg = JSON.parse(fs.readFileSync(pkgJsonPath, 'utf8'));
  if (pkg.version !== EXPECTED_VERSION) {
    throw new Error(
      `[patch-dsh-acp] FATAL: Unsupported @deepseek-ai/dsh-acp version "${pkg.version}". Expected exact version "${EXPECTED_VERSION}". Aborting fail-closed.`
    );
  }

  if (!fs.existsSync(indexPath)) {
    throw new Error(
      `[patch-dsh-acp] FATAL: @deepseek-ai/dsh-acp entrypoint not found at "${indexPath}". Aborting fail-closed.`
    );
  }

  let content = fs.readFileSync(indexPath, 'utf8');

  // 2. Check if already patched
  const hasSteerMethod = content.includes('this.agent.steer(message);');
  const hasImplSteer = content.includes('return requireSession(brandString(params.sessionId)).steer(params);');
  const hasConnSteer = content.includes('.onRequest("session/steer"');

  if (hasSteerMethod && hasImplSteer && hasConnSteer) {
    console.log(`[patch-dsh-acp] Verified intact: @deepseek-ai/dsh-acp@${EXPECTED_VERSION} at ${indexPath} is already patched.`);
    return false;
  }

  // If partially patched or in an unexpected state, fail closed
  if (hasSteerMethod || hasImplSteer || hasConnSteer) {
    throw new Error(
      `[patch-dsh-acp] FATAL: Inconsistent/partial steer patch detected in "${indexPath}". Aborting fail-closed.`
    );
  }

  // 3. Strict code anchor verification
  const cancelTarget = `	cancel() {
		const inflight = this.inflight;
		this.cancelPrompt("ACP prompt cancelled");
		if (inflight === void 0) this.agent.cancel({ kind: "user" });
	}`;

  const promptTarget = `		async prompt(params, requestSignal) {
			assertOpen();
			return requireSession(brandString(params.sessionId)).prompt(params, imagePromptEnabled, requestSignal);
		},`;

  const connTarget = `.onRequest(methods.agent.session.prompt, ({ params, signal }) => implementation.prompt(params, signal))`;

  const cancelCount = countOccurrences(content, cancelTarget);
  if (cancelCount !== 1) {
    throw new Error(
      `[patch-dsh-acp] FATAL: Expected exactly 1 occurrence of cancel() anchor, found ${cancelCount}. Upstream code shape mismatch; aborting fail-closed.`
    );
  }

  const promptCount = countOccurrences(content, promptTarget);
  if (promptCount !== 1) {
    throw new Error(
      `[patch-dsh-acp] FATAL: Expected exactly 1 occurrence of implementation.prompt() anchor, found ${promptCount}. Upstream code shape mismatch; aborting fail-closed.`
    );
  }

  const connCount = countOccurrences(content, connTarget);
  if (connCount !== 1) {
    throw new Error(
      `[patch-dsh-acp] FATAL: Expected exactly 1 occurrence of connection.onRequest(prompt) anchor, found ${connCount}. Upstream code shape mismatch; aborting fail-closed.`
    );
  }

  // 4. Apply patch
  content = content.replace(cancelTarget, `${cancelTarget}\n${STEER_METHOD_CODE}`);
  content = content.replace(promptTarget, `${promptTarget}\n${IMPLEMENTATION_STEER_CODE}`);
  content = content.replace(connTarget, `${connTarget}${CONNECTION_STEER_CODE}`);

  fs.writeFileSync(indexPath, content, 'utf8');

  // 5. Syntax validation via node --check
  try {
    execFileSync(process.execPath, ['--check', indexPath], { stdio: 'pipe' });
    console.log(`[patch-dsh-acp] Successfully patched and syntax-validated: ${indexPath}`);
  } catch (err) {
    throw new Error(`[patch-dsh-acp] FATAL: Node syntax validation failed on "${indexPath}": ${err.message || err}`);
  }

  return true;
}

function main() {
  console.log(`[patch-dsh-acp] Validating project DSH ACP dependency...`);
  verifyAndPatchProjectDshAcp();
}

main();
