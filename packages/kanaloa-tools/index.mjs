import { randomUUID } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';
import manifest from './package.json' with { type: 'json' };
import {
  BlockAssembler, HarnessError, createAssistantMessage, createSystemMessage,
  createToolResultMessage, createUserMessage,
} from '@deepseek-ai/dsh-llm';

export const name = 'kanaloa-tools';
export const inject = ['tools', 'subprocess', 'llm', 'approval', 'sandboxPolicy'];
const IMAGE = `kane-kanaloa-tools:${manifest.version}`;
const WIRE_LIMIT = 1_048_576;
const failure = code => new HarnessError(`Kanaloa Tool: ${code}`, code);
const safeError = code => typeof code === 'string' && /^[A-Z_]+(?::[A-Za-z_]+)?$/u.test(code) ? code : 'PYTHON_INTERNAL_ERROR';
function workerError(frame) {
  const error = failure(safeError(frame.code));
  error.workerErrorType = typeof frame.error_type === 'string' && /^[A-Za-z_]{1,64}$/u.test(frame.error_type) ? frame.error_type : 'unknown';
  return error;
}

function positive(value, fallback, maximum) {
  if (value === undefined) return fallback;
  if (!Number.isInteger(value) || value < 1 || value > maximum) throw failure('INVALID_TOOL_CONFIG');
  return value;
}

export function containerArguments(container, policy) {
  if (!isAbsolute(policy.workspaceRoot) || /[,\r\n]/u.test(policy.workspaceRoot)) throw failure('INVALID_WORKSPACE');
  const readOnly = policy.mode === 'read-only' ? ',readonly' : '';
  return [
    'docker', 'run', '--pull=never', '--rm', '-i', '--name', container,
    '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
    '--pids-limit=64', '--memory=768m', '--memory-swap=768m', '--cpus=1',
    '--tmpfs', '/tmp:rw,nosuid,noexec,size=64m,mode=1777',
    '--mount', `type=bind,source=${policy.workspaceRoot},target=/workspace${readOnly}`,
    '--workdir', '/workspace', IMAGE,
  ];
}

function nativeMessages(messages, route) {
  if (!Array.isArray(messages) || messages.length > 200) throw failure('INVALID_MODEL_REQUEST');
  return messages.map(message => {
    if (!['system', 'user', 'assistant', 'tool'].includes(message.role)) throw failure('INVALID_MODEL_REQUEST');
    let text = message.content ?? '';
    if (Array.isArray(text) && text.every(block => block.type === 'text' && typeof block.text === 'string')) text = text.map(block => block.text).join('\n');
    if (typeof text !== 'string') throw failure('UNSUPPORTED_MODEL_CONTENT');
    if (message.role === 'system') return createSystemMessage(text, name);
    if (message.role === 'tool') {
      if (typeof message.tool_call_id !== 'string') throw failure('INVALID_MODEL_REQUEST');
      return createToolResultMessage({ callId: message.tool_call_id, content: [{ type: 'text', text }], isError: false });
    }
    const content = text ? [{ type: 'text', text }] : [];
    if (message.role === 'assistant') {
      for (const call of message.tool_calls ?? []) {
        if (typeof call.id !== 'string' || typeof call.function?.name !== 'string' || typeof call.function?.arguments !== 'string') throw failure('INVALID_MODEL_REQUEST');
        content.push({ type: 'tool-call', id: call.id, name: call.function.name, arguments: call.function.arguments });
      }
      return createAssistantMessage({ content, source: route });
    }
    return createUserMessage({ content, source: { kind: 'plugin', plugin: name } });
  });
}

async function generate(ctx, frame, exec, budget) {
  if (++budget.calls > budget.maxCalls) throw failure('MODEL_CALL_LIMIT_EXCEEDED');
  const route = { provider: exec.agent.options.provider, model: exec.agent.options.model };
  if (!route.provider || !route.model) throw failure('MODEL_PROVIDER_UNAVAILABLE');
  const payload = frame.payload;
  const messages = nativeMessages(payload.messages, route);
  if (payload.output_schema) messages.push(createSystemMessage(
    `Return JSON matching this schema, without markdown fences: ${JSON.stringify(payload.output_schema)}`, name,
  ));
  const inputBound = Buffer.byteLength(JSON.stringify({ messages, tools: payload.tools }));
  if (budget.tokens + inputBound + 2048 > 32_000) throw failure('MODEL_TOKEN_LIMIT_EXCEEDED');
  const assembler = new BlockAssembler();
  let explicitFinish = false;
  for await (const chunk of ctx.llm.stream({
    ...route, messages, tools: payload.tools, maxTokens: 2048,
    sessionId: exec.agent.session.id, signal: exec.signal,
  })) {
    exec.signal.throwIfAborted();
    if (chunk.type === 'finish') explicitFinish = true;
    assembler.push(chunk);
  }
  exec.signal.throwIfAborted();
  const reason = assembler.finish;
  if (!explicitFinish || !['stop', 'tool-calls'].includes(reason.kind)) throw failure('MODEL_REQUEST_FAILED');
  const blocks = assembler.blocks();
  const content = blocks.filter(block => block.type === 'text').map(block => block.text).join('');
  const tool_calls = blocks.filter(block => block.type === 'tool-call').map(block => ({ id: block.id, name: block.name, arguments: block.arguments }));
  const assistant_message = { role: 'assistant', content };
  if (tool_calls.length) assistant_message.tool_calls = tool_calls.map(call => ({ id: call.id, type: 'function', function: { name: call.name, arguments: call.arguments } }));
  const usage = assembler.usage;
  budget.tokens += usage ? usage.totalTokens ?? usage.inputTokens + usage.outputTokens : inputBound + 2048;
  if (budget.tokens > 32_000) throw failure('MODEL_TOKEN_LIMIT_EXCEEDED');
  return { content, tool_calls, assistant_message, usage: usage ? { prompt_tokens: usage.inputTokens, completion_tokens: usage.outputTokens, total_tokens: usage.totalTokens ?? usage.inputTokens + usage.outputTokens } : null };
}

async function write(handle, frame) {
  const line = JSON.stringify(frame) + '\n';
  if (Buffer.byteLength(line) > WIRE_LIMIT) throw failure('INPUT_LIMIT_EXCEEDED');
  if (!handle.stdin || handle.stdin.destroyed) throw failure('PYTHON_SESSION_UNAVAILABLE');
  await new Promise((resolve, reject) => handle.stdin.write(line, error => error ? reject(error) : resolve()));
}

async function* frames(stream) {
  let buffered = Buffer.alloc(0);
  for await (const chunk of stream) {
    buffered = Buffer.concat([buffered, chunk]);
    if (buffered.length > WIRE_LIMIT) throw failure('OUTPUT_LIMIT_EXCEEDED');
    let boundary;
    while ((boundary = buffered.indexOf(10)) !== -1) {
      const line = buffered.subarray(0, boundary).toString('utf8');
      buffered = buffered.subarray(boundary + 1);
      yield line;
    }
  }
}

export function apply(ctx, config = {}) {
  if (config.enabled === false || process.env.KANALOA_TOOLS_ENABLED === '0') return;
  const maxSessions = positive(config.maxSessions, 8, 64);
  const timeoutMs = positive(config.timeoutMs, 120_000, 600_000);
  const idleMs = positive(config.idleMs, 1_800_000, 86_400_000);
  const maxModelCalls = positive(config.maxModelCalls, 8, 16);
  const sessions = new Map();
  let closing = false;

  async function removeContainer(entry) {
    if (entry.removing) return entry.removing;
    entry.removing = (async () => {
      entry.handle?.stdin?.end();
      let stopped = !entry.handle;
      try {
        entry.handle?.terminate();
        if (entry.handle) stopped = await entry.handle.waitForExit(AbortSignal.timeout(5000));
      } catch { /* Still remove the owned container if CLI process observation failed. */ }
      const deadline = AbortSignal.timeout(15_000);
      while (true) {
        deadline.throwIfAborted();
        const removal = ctx.subprocess.spawn({
          argv: ['docker', 'container', 'rm', '--force', entry.container], cwd: entry.policy.workspaceRoot,
          stdio: { stdin: 'ignore', stdout: { maxBytes: 1024 }, stderr: { maxBytes: 1024 } }, graceMs: 1000,
          signal: deadline,
        });
        const result = await removal.done;
        if (result.exitCode === 0) break;
        const stderr = removal.collected.stderr?.readFrom(0).text ?? '';
        if (/No such container/iu.test(stderr)) break;
        // --rm may already own removal after docker run exits. Confirm absence, not success by assumption.
        if (!/removal of container .* is already in progress/iu.test(stderr)) throw failure('SUBTASK_CLEANUP_FAILED');
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      if (!stopped && !await entry.handle.waitForExit(AbortSignal.timeout(5000))) throw failure('SUBTASK_CLEANUP_FAILED');
      entry.handle?.stdout?.destroy();
    })();
    return entry.removing;
  }

  function worker(entry) {
    return { policy: entry.policy, container: `kane-kanaloa-${randomUUID()}` };
  }

  async function removeSession(entry) {
    entry.removing = true;
    const results = await Promise.allSettled([entry.kernel, entry.nooa].filter(Boolean).map(removeContainer));
    if (results.some(result => result.status === 'rejected')) throw failure('SUBTASK_CLEANUP_FAILED');
    if (sessions.get(entry.id) === entry) sessions.delete(entry.id);
  }

  async function start(entry) {
    entry.handle = ctx.subprocess.spawn({
      argv: containerArguments(entry.container, entry.policy), cwd: entry.policy.workspaceRoot,
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: { maxBytes: 2048 } }, graceMs: 1000,
    });
    entry.handle.done.catch(() => {});
    entry.handle.stdin.on('error', () => {});
    entry.frames = frames(entry.handle.stdout);
    let frame;
    try { frame = await nextFrame(entry); } catch { throw failure('NOOA_WORKER_START_FAILED'); }
    if (frame.type !== 'ready') throw failure('NOOA_WORKER_START_FAILED');
  }

  async function nextFrame(entry) {
    const next = await Promise.race([
      entry.frames.next(),
      entry.handle.done.then(() => { throw failure('PYTHON_SESSION_LOST'); }, () => { throw failure('NOOA_WORKER_START_FAILED'); }),
    ]);
    if (next.done || Buffer.byteLength(next.value) > WIRE_LIMIT) throw failure('PYTHON_SESSION_LOST');
    let frame;
    try { frame = JSON.parse(next.value); } catch { throw failure('INVALID_WORKER_FRAME'); }
    if (!frame || typeof frame !== 'object') throw failure('INVALID_WORKER_FRAME');
    return frame;
  }

  async function execute(kind, args, original) {
    const allowed = kind === 'ipython' ? ['code'] : ['goal', 'strategy', 'output_contract', 'python_state'];
    if (Object.keys(args).some(key => !allowed.includes(key))) throw failure('INVALID_TOOL_ARGS');
    const text = kind === 'ipython' ? args.code : args.goal;
    if (typeof text !== 'string' || !text.trim() || text.length > (kind === 'ipython' ? 65_536 : 32_768)) throw failure('INVALID_TOOL_ARGS');
    if (kind === 'nooa' && (config.nooaEnabled === false || process.env.KANALOA_NOOA_ENABLED === '0')) throw failure('NOOA_DISABLED');
    if (!original.agent?.session?.id) throw failure('SESSION_BOUNDARY_VIOLATION');
    original.signal.throwIfAborted();
    const approval = await ctx.approval.request({
      agent: original.agent, callId: original.callId, toolName: original.name, signal: original.signal,
      reason: 'Run Python in this session\'s offline container. The current workspace, including any secrets stored there, is readable and may be writable under its existing policy. No other host paths or environment credentials are forwarded.',
    });
    if (approval !== 'allowed-once') throw failure(approval === 'cancelled' ? 'SUBTASK_CANCELLED' : 'PERMISSION_DENIED');
    original.signal.throwIfAborted();
    const policy = ctx.sandboxPolicy.resolve({ session: original.agent.session });
    const id = original.agent.session.id;
    let entry = sessions.get(id);
    if (entry && (entry.agent !== original.agent || entry.policy.workspaceRoot !== policy.workspaceRoot || entry.policy.mode !== policy.mode)) {
      await removeSession(entry);
      throw failure('SESSION_BOUNDARY_VIOLATION');
    }
    if (entry?.busy || entry?.removing) throw failure('PYTHON_SESSION_BUSY');
    if (!entry) {
      if (closing || sessions.size >= maxSessions) throw failure('RESOURCE_LIMIT_EXCEEDED');
      entry = { id, agent: original.agent, policy, busy: true, lastUsed: Date.now() };
      sessions.set(id, entry);
    }
    entry.busy = true;
    const deadline = AbortSignal.timeout(timeoutMs);
    const signal = AbortSignal.any([original.signal, deadline]);
    const exec = { ...original, signal };
    const budget = { calls: 0, maxCalls: maxModelCalls, tokens: 0 };
    let aborted;
    const cancelled = new Promise((_, reject) => {
      aborted = () => reject(failure(deadline.aborted ? 'SUBTASK_TIMEOUT' : 'SUBTASK_CANCELLED'));
      signal.addEventListener('abort', aborted, { once: true });
      if (signal.aborted) aborted();
    });
    const runPython = async code => {
      if (typeof code !== 'string' || !code.trim() || code.length > 65_536) throw failure('INVALID_TOOL_ARGS');
      entry.kernel ??= worker(entry);
      if (!entry.kernel.handle) await start(entry.kernel);
      signal.throwIfAborted();
      await write(entry.kernel.handle, { type: 'ipython', payload: { code } });
      const frame = await nextFrame(entry.kernel);
      signal.throwIfAborted();
      if (frame.type === 'error') throw workerError(frame);
      if (frame.type !== 'result' || typeof frame.payload?.stdout !== 'string' || (frame.payload.value !== null && typeof frame.payload.value !== 'string')) throw failure('OUTPUT_VALIDATION_FAILED');
      return frame.payload;
    };
    const work = async () => {
      if (kind === 'ipython') return { ...await runPython(args.code), model_calls: 0, tokens: 0 };
      entry.nooa = worker(entry);
      const process = entry.nooa;
      await start(process);
      signal.throwIfAborted();
      await write(process.handle, { type: kind, payload: { ...args, max_model_calls: maxModelCalls } });
      while (true) {
        const frame = await nextFrame(process);
        signal.throwIfAborted();
        if (frame.type === 'model' && kind === 'nooa') {
          if (frame.id !== budget.calls + 1) throw failure('INVALID_WORKER_FRAME');
          try { await write(process.handle, { type: 'model.result', id: frame.id, payload: await generate(ctx, frame, exec, budget) }); }
          catch (error) {
            signal.throwIfAborted();
            await write(process.handle, { type: 'model.result', id: frame.id, error: error instanceof HarnessError ? error.code : 'MODEL_REQUEST_FAILED' });
          }
        } else if (frame.type === 'ipython.call') {
          if (args.python_state === 'ephemeral') throw failure('PYTHON_SESSION_UNAVAILABLE');
          await write(process.handle, { type: 'ipython.result', id: frame.id, payload: await runPython(frame.payload?.code) });
        } else if (frame.type === 'result') {
          const result = frame.payload;
          if (typeof result?.result?.answer !== 'string' || !result.result.answer || !Array.isArray(result.result.facts) || result.result.facts.some(fact => typeof fact !== 'string')) throw failure('OUTPUT_VALIDATION_FAILED');
          await removeContainer(process);
          entry.nooa = null;
          return { ...result, model_calls: budget.calls, tokens: budget.tokens };
        } else if (frame.type === 'error') {
          throw workerError(frame);
        } else throw failure('INVALID_WORKER_FRAME');
      }
    };
    const started = Date.now();
    let outcome = 'completed';
    let errorType;
    try { return await Promise.race([work(), cancelled]); }
    catch (error) {
      outcome = error instanceof HarnessError ? error.code : 'PYTHON_INTERNAL_ERROR';
      errorType = error.workerErrorType;
      // An uncertain cell is never retried; killing the container also discards mutable state.
      try { await removeSession(entry); }
      catch { outcome = 'SUBTASK_CLEANUP_FAILED'; throw failure(outcome); }
      throw error instanceof HarnessError ? error : failure(outcome);
    } finally {
      signal.removeEventListener('abort', aborted);
      entry.busy = false;
      entry.lastUsed = Date.now();
      if (!entry.kernel && !entry.nooa && sessions.get(id) === entry) sessions.delete(id);
      process.stderr.write(JSON.stringify({ source: name, event: 'tool_end', session_ref: id, tool: kind,
        outcome, error_type: errorType, duration_ms: Date.now() - started, model_calls: budget.calls, tokens: budget.tokens }) + '\n');
    }
  }

  const output = {
    schema: { type: 'json' },
    render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
  };
  ctx.tools.register(defineTool({
    name: 'ipython',
    description: 'Execute Python in a persistent IPython belonging only to the current native session. Variables survive successful calls; other sessions and branches are isolated. Offline, resource-limited container with approved workspace access. Cancel, crash or idle expiry discards state; never retry an unknown side effect automatically.',
    parameters: { code: { type: 'string', required: true } },
    output, execute: (args, exec) => execute('ipython', args, exec),
  }));
  if (config.nooaEnabled !== false && process.env.KANALOA_NOOA_ENABLED !== '0') ctx.tools.register(defineTool({
    name: 'nooa_execute',
    description: 'Solve one local subtask with NOOA typed Python objects. Default auto/predict returns a validated answer/facts contract. Select codeact for bounded Python object-method composition; self.ipython(code) accesses this native session\'s persistent IPython unless python_state=ephemeral. Uses the current Agent\'s model through DSH, with extra model calls; does not start another top-level Turn or Loop. Requires the installed offline Kanaloa Tool image.',
    parameters: {
      goal: { type: 'string', required: true },
      strategy: { type: 'string', enum: ['auto', 'predict', 'codeact'] },
      output_contract: { type: 'string', enum: ['answer'] },
      python_state: { type: 'string', enum: ['auto', 'ephemeral', 'reuse_session'] },
    },
    output, execute: (args, exec) => execute('nooa', args, exec),
  }));
  ctx.on('agent/disposed', async ({ agent }) => {
    const entry = sessions.get(agent.session.id);
    if (entry) await removeSession(entry);
  });
  ctx.effect(() => {
    const timer = setInterval(() => {
      for (const entry of sessions.values()) if (!entry.busy && Date.now() - entry.lastUsed >= idleMs) {
        void removeSession(entry).catch(() => {
          process.stderr.write(JSON.stringify({ source: name, event: 'cleanup_failed', code: 'SUBTASK_CLEANUP_FAILED' }) + '\n');
        });
      }
    }, Math.min(idleMs, 30_000));
    timer.unref();
    return async () => {
      closing = true;
      clearInterval(timer);
      const results = await Promise.allSettled([...sessions.values()].map(removeSession));
      if (results.some(result => result.status === 'rejected')) throw failure('SUBTASK_CLEANUP_FAILED');
    };
  });
}
