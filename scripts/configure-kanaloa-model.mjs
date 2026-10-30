import { readFileSync } from 'node:fs';
import { mkdir, readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import YAML from 'yaml';
import { parseCredentialsDocument, resolveSpec as resolveCredentialsSpec } from '@deepseek-ai/dsh-credentials-local';
import { resolveSpec as resolveSettingsSpec } from '@deepseek-ai/dsh-settings-file';
import { withFileLock, writeFileAtomic } from '@deepseek-ai/dsh-atomic-write';

const input = JSON.parse(readFileSync(0, 'utf8'));
const { base_url: baseURL, model, api_format: apiFormat, api_key: apiKey } = input ?? {};
const apiKeyRef = `KANE_KANALOA_API_KEY_${randomUUID().replaceAll('-', '')}`;
const provider = 'kane-kanaloa';

function validBaseURL(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash;
  } catch {
    return false;
  }
}

if (
  typeof baseURL !== 'string' || !validBaseURL(baseURL) || baseURL.length > 2048 ||
  typeof model !== 'string' || model.trim().length === 0 || model.length > 256 || /[\u0000-\u001f]/u.test(model) ||
  !['openai-completions', 'openai-responses', 'anthropic-messages'].includes(apiFormat) ||
  typeof apiKey !== 'string' || apiKey.trim().length === 0 || apiKey.length > 4096
) {
  process.stderr.write('invalid_model_configuration\n');
  process.exitCode = 2;
} else if (process.env.KANE_KANALOA_API_KEY) {
  process.stderr.write('credential_is_overridden_by_process_environment\n');
  process.exitCode = 3;
} else {
  const settingsPath = resolveSettingsSpec({}).filename;
  const credentialsPath = resolveCredentialsSpec({}).filename;
  const profile = {
    apiKeyEnv: apiKeyRef,
    api: apiFormat,
    baseURL: baseURL.trim().replace(/\/+$/u, ''),
    models: [{ id: model.trim() }],
  };

  await mkdir(dirname(credentialsPath), { recursive: true, mode: 0o700 });
  await mkdir(dirname(settingsPath), { recursive: true, mode: 0o700 });
  // A new reference keeps the active profile/key pair intact if settings commit fails.
  await withFileLock(settingsPath, async () => {
    const document = await readDocument(settingsPath, '{}\n');
    if (!YAML.isMap(document.contents)) throw new Error('settings_document_root_invalid');
    ensureObjectPath(document, ['llm-pi-ai', 'providers']);
    document.setIn(['llm-pi-ai', 'providers', provider], document.createNode(profile));
    document.set('agent-default-model', document.createNode({ provider, model: model.trim() }));
    if (document.errors.length) throw new Error('settings_document_invalid');
    await withFileLock(credentialsPath, async () => {
      const credentials = await readDocument(credentialsPath, 'version: 1\nrefs: {}\n');
      const previous = credentials.toString();
      parseCredentialsDocument(previous, credentialsPath);
      if (!YAML.isMap(credentials.contents)) throw new Error('credentials_document_root_invalid');
      ensureObjectPath(credentials, ['refs']);
      credentials.setIn(['refs', apiKeyRef], apiKey.trim());
      await writeFileAtomic(credentialsPath, credentials.toString(), { mode: 0o600, dirMode: 0o700 });
      try {
        await writeFileAtomic(settingsPath, document.toString(), { mode: 0o600, dirMode: 0o700 });
      } catch (error) {
        await writeFileAtomic(credentialsPath, previous, { mode: 0o600, dirMode: 0o700 });
        throw error;
      }
    }, { waitMs: 10000 });
  }, { waitMs: 10000 });

  process.stdout.write('{"status":"saved"}\n');
}

async function readDocument(path, fallback) {
  try {
    const document = YAML.parseDocument(await readFile(path, 'utf8'), { uniqueKeys: true });
    if (document.errors.length) throw new Error('yaml_document_invalid');
    return document;
  } catch (error) {
    if (error?.code === 'ENOENT') return YAML.parseDocument(fallback, { uniqueKeys: true });
    throw error;
  }
}

function ensureObjectPath(document, path) {
  let value = document.toJSON();
  for (let index = 0; index < path.length; index += 1) {
    const key = path[index];
    const child = value?.[key];
    if (child === undefined) {
      document.setIn(path.slice(0, index + 1), document.createNode({}));
      value = {};
    } else if (typeof child !== 'object' || child === null || Array.isArray(child)) {
      throw new Error('settings_namespace_invalid');
    } else {
      value = child;
    }
  }
}
