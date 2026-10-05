#!/usr/bin/env node
/**
 * scripts/patch-dsh-acp.mjs
 * 
 * Formal, reproducible build/setup script for Kanaloa's downstream DSH Steer Extension.
 * Applies Kanaloa's verified extensions to project-local @deepseek-ai/dsh-acp:
 * 1. AcpSession.prototype.steer(params) -> native agent.steer(message)
 * 2. connection.onRequest("session/steer", ...) and onNotification("session/steer", ...)
 * 3. Preserve the native turn/end kind in ACP PromptResponse._meta.
 * 4. Use the saved Runtime default model for new ACP sessions.
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

  const steerInstalled = hasSteerMethod && hasImplSteer && hasConnSteer;
  if (!steerInstalled && (hasSteerMethod || hasImplSteer || hasConnSteer)) {
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

  let changed = false;
  if (!steerInstalled) {
    for (const [anchor, label] of [[cancelTarget, 'cancel()'], [promptTarget, 'implementation.prompt()'], [connTarget, 'connection.onRequest(prompt)']]) {
      if (countOccurrences(content, anchor) !== 1) {
        throw new Error(`[patch-dsh-acp] FATAL: Expected exactly 1 ${label} anchor; aborting fail-closed.`);
      }
    }
    content = content.replace(cancelTarget, `${cancelTarget}\n${STEER_METHOD_CODE}`);
    content = content.replace(promptTarget, `${promptTarget}\n${IMPLEMENTATION_STEER_CODE}`);
    content = content.replace(connTarget, `${connTarget}${CONNECTION_STEER_CODE}`);
    changed = true;
  }

  // ACP's deployment defaults otherwise override the user's saved model route.
  const services = 'const inject = [\n\t"agents",';
  const oldSavedServices = 'const inject = [\n\t"agentDefaultModel",\n\t"agents",';
  const savedServices = 'const inject = [\n\t"settings",\n\t"credentials",\n\t"agentDefaultModel",\n\t"agents",';
  if (countOccurrences(content, oldSavedServices) === 1) content = content.replace(oldSavedServices, services);
  const routeAnchors = [
    ['agentOptions: agentOptions(config),', 'agentOptions: ctx.agentDefaultModel.currentSelection(),', 2],
    ['fallbackSelection: initialSelection(config),', 'fallbackSelection: ctx.agentDefaultModel.currentSelection(),', 2],
    [services, savedServices, 1],
  ];
  for (const [before, after, expected] of routeAnchors) {
    if (countOccurrences(content, after) === expected && countOccurrences(content, before) === 0) continue;
    if (countOccurrences(content, before) !== expected || countOccurrences(content, after) !== 0) {
      throw new Error('[patch-dsh-acp] FATAL: Saved-model routing anchors changed; aborting fail-closed.');
    }
    content = content.replaceAll(before, after);
    changed = true;
  }
  const newSessionAnchor = '\t\tasync newSession(params, signal) {\n\t\t\tassertOpen();\n\t\t\tvalidateWorkspaceParams(params);';
  const configuredSession = `${newSessionAnchor}\n\t\t\tconst savedModel = ctx.settings.describe().find((section) => section.ns === "agent-default-model")?.user;\n\t\t\tif (!savedModel?.provider || !savedModel?.model) throw invalidParams("Kanaloa model is not configured; save Base URL, Model and API Key in Kane first.");`;
  if (countOccurrences(content, configuredSession) !== 1) {
    if (countOccurrences(content, newSessionAnchor) !== 1) throw new Error('[patch-dsh-acp] FATAL: Model configuration guard anchor changed.');
    content = content.replace(newSessionAnchor, configuredSession);
    changed = true;
  }

  const nativeResult = 'return { stopReason: await completion.promise, _meta: { kaneNativeEndKind: inflight.endReason?.kind ?? "unknown", ...(inflight.endReason?.kind === "error" ? { kaneNativeErrorCode: typeof inflight.endReason.error?.code === "string" && /^[A-Za-z0-9_.-]{1,64}$/.test(inflight.endReason.error.code) ? inflight.endReason.error.code : void 0, kaneNativeErrorStatus: Number.isInteger(inflight.endReason.error?.status) && inflight.endReason.error.status >= 100 && inflight.endReason.error.status <= 599 ? inflight.endReason.error.status : void 0, kaneNativeErrorSource: typeof inflight.endReason.error?.source === "string" && /^[A-Za-z0-9_.-]{1,64}$/.test(inflight.endReason.error.source) ? inflight.endReason.error.source : void 0 } : {}) } };';
  const legacyNativeResult = 'return { stopReason: await completion.promise, _meta: { kaneNativeEndKind: inflight.endReason?.kind ?? "unknown" } };';
  const upstreamResult = 'return { stopReason: await completion.promise };';
  const upstreamError = 'else if (end.kind === "error") inflight.reject(internalError$1(`turn failed: ${end.error.message}`));';
  const nativeError = 'else if (end.kind === "error") inflight.resolve(turnEndToStopReason(end));';
  const resultInstalled = countOccurrences(content, nativeResult);
  const legacyResultInstalled = countOccurrences(content, legacyNativeResult);
  const errorInstalled = countOccurrences(content, nativeError);
  if (resultInstalled === 2 && errorInstalled === 1) {
    // Already installed, including when postinstall runs after setup:dsh.
  } else if (resultInstalled === 0 && legacyResultInstalled === 2 && errorInstalled === 1) {
    content = content.replaceAll(legacyNativeResult, nativeResult);
    changed = true;
  } else if (resultInstalled !== 0 || errorInstalled !== 0) {
    throw new Error('[patch-dsh-acp] FATAL: Partial native-outcome patch detected; aborting fail-closed.');
  } else {
    if (countOccurrences(content, upstreamResult) !== 2 || countOccurrences(content, upstreamError) !== 1) {
      throw new Error('[patch-dsh-acp] FATAL: Native-outcome anchors changed; aborting fail-closed.');
    }
    content = content.replaceAll(upstreamResult, nativeResult);
    content = content.replace(upstreamError, nativeError);
    changed = true;
  }

  if (!changed) {
    console.log(`[patch-dsh-acp] Verified intact: @deepseek-ai/dsh-acp@${EXPECTED_VERSION} at ${indexPath} is already patched.`);
    return false;
  }

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
