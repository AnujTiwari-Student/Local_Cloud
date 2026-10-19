// Test-only transport adapter. Production example tests always use actual HTTP.
import { after } from 'node:test';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

let worker, lines, nextId = 0;
const pending = new Map();
globalThis.fetch = async (url, options) => {
  if (!worker) {
    worker = spawn(process.env.LOCALCLOUD_TEST_PYTHON ?? 'python3', [
      fileURLToPath(new URL('./node_http_bridge.py', import.meta.url)),
      fileURLToPath(new URL('../../examples/node-app/localcloud.toml', import.meta.url)),
    ], { stdio: ['pipe', 'pipe', 'inherit'] });
    worker.on('error', error => { for (const waiter of pending.values()) waiter.reject(error); pending.clear(); });
    worker.on('exit', code => { if (pending.size) { for (const waiter of pending.values()) waiter.reject(new Error('Python bridge exited: ' + code)); pending.clear(); } });
    lines = createInterface({ input: worker.stdout });
    lines.on('line', line => {
      const result = JSON.parse(line), waiter = pending.get(result.id);
      pending.delete(result.id);
      waiter.resolve(new Response(result.status === 204 ? null : Buffer.from(result.body, 'base64'), { status: result.status, headers: result.headers }));
    });
  }
  const id = nextId++;
  const promise = new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  worker.stdin.write(JSON.stringify({ id, url, method: options.method, headers: options.headers, body: Buffer.from(options.body ?? '').toString('base64') }) + '\n');
  return promise;
};
after(async () => {
  if (worker && worker.exitCode === null) {
    const exited = once(worker, 'exit');
    worker.stdin.end();
    await exited;
  }
  lines?.close();
});
process.env.LOCALCLOUD_CI = '1';
process.env.LOCALCLOUD_URL = 'http://127.0.0.1:4566';
process.env.LOCALCLOUD_MANAGEMENT_TOKEN = 'bridge-test-token';
process.env.LOCALCLOUD_PRINCIPAL = 'app';
await import('../../examples/node-app/test/integration.test.mjs');
