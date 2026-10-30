import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import manifest from './package.json' with { type: 'json' };

try {
  const platform = execFileSync('docker', ['info', '--format', '{{.OSType}}'], {
    encoding: 'utf8', timeout: 10_000, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
  if (platform !== 'linux') throw new Error('linux_engine_required');
} catch {
  console.error('Kanaloa Tools: Docker Linux engine is unavailable. Start/fix Docker Desktop first. No image was built; no host-Python fallback is enabled.');
  process.exit(1);
}
const result = spawnSync('docker', ['build', '--tag', `kane-kanaloa-tools:${manifest.version}`, '.'], {
  cwd: fileURLToPath(new URL('.', import.meta.url)), stdio: 'inherit', windowsHide: true,
});
process.exit(result.status ?? 1);
