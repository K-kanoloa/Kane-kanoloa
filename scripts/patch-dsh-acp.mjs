#!/usr/bin/env node
/**
 * scripts/patch-dsh-acp.mjs
 * 
 * Formal, reproducible build/setup script for Kanaloa's downstream DSH Steer Extension.
 * Applies the verified Native Steer extension to @deepseek-ai/dsh-acp:
 * 1. AcpSession.prototype.steer(params) -> native agent.steer(message)
 * 2. connection.onRequest("session/steer", ...) and onNotification("session/steer", ...)
 * 
 * Target locations:
 * - Local repository node_modules/@deepseek-ai/dsh-acp/lib/index.js
 * - Global/npx cache node_modules/@deepseek-ai/dsh-acp/lib/index.js (if present)
 * 
 * Idempotent: safe to run multiple times, across clean installs, npm setup, or postinstall.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, '..');

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

function findDshAcpIndexFiles() {
  const candidates = [];

  // 1. Local repository node_modules
  const localIndex = path.join(repoRoot, 'node_modules', '@deepseek-ai', 'dsh-acp', 'lib', 'index.js');
  if (fs.existsSync(localIndex)) {
    candidates.push(localIndex);
  }

  // 2. Windows npm-cache / _npx cache
  const localAppData = process.env.LOCALAPPDATA;
  if (localAppData) {
    const npxCacheDir = path.join(localAppData, 'npm-cache', '_npx');
    if (fs.existsSync(npxCacheDir)) {
      try {
        const subdirs = fs.readdirSync(npxCacheDir);
        for (const sub of subdirs) {
          const cacheIndex = path.join(npxCacheDir, sub, 'node_modules', '@deepseek-ai', 'dsh-acp', 'lib', 'index.js');
          if (fs.existsSync(cacheIndex)) {
            candidates.push(cacheIndex);
          }
        }
      } catch {
        // ignore scan errors
      }
    }
  }

  return candidates;
}

function patchFile(filePath) {
  let content = fs.readFileSync(filePath, 'utf8');

  // Check if already patched
  if (content.includes('session/steer') && content.includes('this.agent.steer(message)')) {
    console.log(`[patch-dsh-acp] Already patched: ${filePath}`);
    return false;
  }

  // 1. Insert steer method into AcpSession class
  const cancelTarget = `	cancel() {
		const inflight = this.inflight;
		this.cancelPrompt("ACP prompt cancelled");
		if (inflight === void 0) this.agent.cancel({ kind: "user" });
	}`;

  if (!content.includes(cancelTarget)) {
    throw new Error(`[patch-dsh-acp] Could not locate cancel() anchor in ${filePath}`);
  }

  content = content.replace(cancelTarget, `${cancelTarget}\n${STEER_METHOD_CODE}`);

  // 2. Insert steer handler in implementation object
  const promptTarget = `		async prompt(params, requestSignal) {
			assertOpen();
			return requireSession(brandString(params.sessionId)).prompt(params, imagePromptEnabled, requestSignal);
		},`;

  if (!content.includes(promptTarget)) {
    throw new Error(`[patch-dsh-acp] Could not locate implementation.prompt() anchor in ${filePath}`);
  }

  content = content.replace(promptTarget, `${promptTarget}\n${IMPLEMENTATION_STEER_CODE}`);

  // 3. Insert onRequest("session/steer") into connection chain
  const connTarget = `.onRequest(methods.agent.session.prompt, ({ params, signal }) => implementation.prompt(params, signal))`;

  if (!content.includes(connTarget)) {
    throw new Error(`[patch-dsh-acp] Could not locate connection.onRequest(prompt) anchor in ${filePath}`);
  }

  content = content.replace(connTarget, `${connTarget}${CONNECTION_STEER_CODE}`);

  fs.writeFileSync(filePath, content, 'utf8');

  // Syntax validation
  try {
    execFileSync(process.execPath, ['--check', filePath], { stdio: 'pipe' });
    console.log(`[patch-dsh-acp] Successfully patched and validated: ${filePath}`);
  } catch (err) {
    throw new Error(`[patch-dsh-acp] Syntax check failed on ${filePath}: ${err}`);
  }

  return true;
}

function main() {
  const targets = findDshAcpIndexFiles();
  if (targets.length === 0) {
    console.warn('[patch-dsh-acp] No @deepseek-ai/dsh-acp installations found to patch yet.');
    return;
  }

  console.log(`[patch-dsh-acp] Found ${targets.length} target(s) for Kanaloa DSH steer extension.`);
  let patchedCount = 0;
  for (const target of targets) {
    if (patchFile(target)) {
      patchedCount++;
    }
  }
  console.log(`[patch-dsh-acp] Done. ${patchedCount} target(s) newly patched.`);
}

main();
