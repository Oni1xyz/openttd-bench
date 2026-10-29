// Run after a production build, inside a disposable network-none container.
// Never reads an existing OUTPUTS_DIR: all fixtures below are synthetic.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, rm, readFile } from 'node:fs/promises';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { once } from 'node:events';
const root = await mkdtemp(path.join(tmpdir(), 'viewer-runtime-'));
const filename = `${'d'.repeat(64)}.atif.json`;
const dir = path.join(root, 'canary/trajectories');
await mkdir(dir, { recursive: true });
const trajectory = { schema_version: 'ATIF-v1.8', trajectory_id: 'canary', agent: { name: 'synthetic', version: '1' },
  steps: [{ step_id: 1, source: 'agent', message: 'local read works' }], extra: { source: { secret: 'not-for-presentation' } } };
await writeFile(path.join(dir, filename), JSON.stringify(trajectory));
await writeFile(path.join(dir, 'index.json'), JSON.stringify({ format: 'openttd-atif-index-v1', trajectories: [{ file: filename, episode_id: 'canary', size: 1 }] }));
const port = 32719;
const host = `127.0.0.1:${port}`;
function get(route, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port, path: route, headers: { host, ...headers } }, res => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', chunk => { text += chunk; if (text.length > 1024 * 1024) req.destroy(new Error('Canary response too large')); });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, text }));
      res.on('error', reject);
    });
    req.setTimeout(2000, () => req.destroy(new Error('Canary request timed out')));
    req.on('error', reject); req.end();
  });
}
for (const mode of ['dev', 'start']) {
  const blocked = spawnSync('npm', ['run', mode, '--', '--hostname=0.0.0.0'], { encoding: 'utf8', timeout: 5000, killSignal: 'SIGKILL', maxBuffer: 32 * 1024 });
  assert.equal(blocked.error, undefined);
  assert.notEqual(blocked.status, 0);
  assert.match(blocked.stderr, /loopback/);
}
const child = spawn(process.execPath, ['scripts/local-next.mjs', 'start', '--port', String(port)], {
  env: { ...process.env, OUTPUTS_DIR: root, NEXT_TELEMETRY_DISABLED: '1', HOSTNAME: '0.0.0.0' }, stdio: ['ignore', 'pipe', 'pipe'],
});
let log = '';
for (const stream of [child.stdout, child.stderr]) stream.on('data', data => { log = (log + data).slice(-16000); });
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) throw new Error(`Viewer exited: ${log}`);
    try { if ((await get('/api/runs')).status === 200) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.equal(ready, true, log);
  const sockets = (await readFile('/proc/net/tcp', 'utf8')).split('\n').slice(1)
    .map(line => line.trim().split(/\s+/)).filter(fields => fields[3] === '0A' && fields[1]?.endsWith(`:${port.toString(16).toUpperCase()}`));
  assert.equal(sockets.length, 1, 'expected one IPv4 listening socket');
  assert.equal(sockets[0][1].split(':')[0], '0100007F', 'listener must bind 127.0.0.1, not all interfaces');
  const listing = await get('/api/runs');
  assert.equal(listing.status, 200);
  assert.equal(listing.headers['cache-control'], 'no-store');
  assert.equal(listing.headers['access-control-allow-origin'], undefined);
  assert.equal(listing.text.includes(root), false);
  const data = JSON.parse(listing.text);
  assert.equal(data.root, undefined);
  assert.deepEqual(data.runs.map(r => r.name), ['canary']);
  const route = '/api/traces?path=canary%2Ftrajectories%2Findex.json';
  const local = await get(route, { origin: `http://${host}`, 'sec-fetch-site': 'same-origin' });
  assert.equal(local.status, 200);
  assert.equal(JSON.parse(local.text)[0].steps[0].message, 'local read works');
  assert.equal(local.text.includes('not-for-presentation'), false);
  let denials = 0;
  for (const api of ['/api/runs', route]) {
    for (const headers of [{ host: 'rebind.attacker.invalid' }, { origin: 'https://attacker.invalid' }, { origin: 'null' },
      { host: 'rebind.attacker.invalid', 'x-forwarded-host': host }, { 'sec-fetch-site': 'cross-site' }, { origin: `http://localhost:${port}` }]) {
      const denied = await get(api, headers);
      assert.equal(denied.status, 403, `${api}: ${JSON.stringify(headers)} ${denied.text}`);
      assert.equal(denied.text.includes(root), false);
      assert.equal(denied.text.includes('local read works'), false);
      denials++;
    }
  }
  for (const route of ['/api/traces?path=missing%2Ftrajectories%2Findex.json', '/api/traces?path=..%2Fcanary%2Ftrajectories%2Findex.json']) {
    const result = await get(route);
    assert.equal(result.status, 404);
    assert.equal(result.text.includes(root), false);
    assert.equal(result.text.includes('ENOENT'), false);
  }
  await rm(root, { recursive: true, force: true });
  const unavailable = await get('/api/runs');
  assert.equal(unavailable.status, 503);
  assert.deepEqual(JSON.parse(unavailable.text), { error: 'Run listing unavailable' });
  assert.equal((await get('/')).status, 200);
  console.log(`Production canary passed: loopback launch; local listing/read; ${denials} hostile requests denied; sanitized failures; page served.`);
} finally {
  child.kill('SIGTERM');
  const timer = setTimeout(() => child.kill('SIGKILL'), 3000);
  if (child.exitCode === null) await once(child, 'exit');
  clearTimeout(timer);
  await rm(root, { recursive: true, force: true });
}
