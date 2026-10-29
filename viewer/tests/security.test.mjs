import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, writeFile, rm, symlink } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { promises as fs } from 'node:fs';
import { load } from './load-ts.mjs';

const trace = await load('../lib/trace.ts');
const server = await load('../lib/server-runs.ts');
const doc = () => ({ schema_version: 'ATIF-v1.8', agent: { name: 'synthetic', version: '1' },
  steps: [{ step_id: 1, source: 'agent', message: 'canary' }] });
const filename = `${'b'.repeat(64)}.atif.json`;
const rel = 'run/trajectories/index.json';
async function fixture(fn) {
  const root = await mkdtemp(path.join(tmpdir(), 'viewer-security-'));
  const old = process.env.OUTPUTS_DIR;
  process.env.OUTPUTS_DIR = root;
  const dir = path.join(root, 'run/trajectories');
  await mkdir(dir, { recursive: true });
  const item = { file: filename, episode_id: 'e', size: 1 };
  const index = async (items = [item]) => writeFile(path.join(dir, 'index.json'), JSON.stringify({ format: 'openttd-atif-index-v1', trajectories: items }));
  try {
    await index();
    await writeFile(path.join(dir, filename), JSON.stringify(doc()));
    await fn({ root, dir, item, index });
  } finally {
    if (old === undefined) delete process.env.OUTPUTS_DIR; else process.env.OUTPUTS_DIR = old;
    await rm(root, { recursive: true, force: true });
  }
}

test('discovery rechecks directories after an ancestor swap before leaf lstat', () => fixture(async ({ root, dir, item }) => {
  const outside = path.join(root, 'outside');
  await mkdir(outside);
  await writeFile(path.join(outside, 'index.json'), JSON.stringify({ format: 'openttd-atif-index-v1', trajectories: [{ ...item, episode_id: 'escaped' }] }));
  const original = fs.lstat;
  let swapped = false;
  fs.lstat = async (name, ...args) => {
    if (name === path.join(dir, 'index.json') && !swapped) {
      swapped = true;
      await fs.rename(dir, `${dir}-old`);
      await fs.symlink(outside, dir);
    }
    return original(name, ...args);
  };
  try { assert.deepEqual(await server.listRuns(), []); assert.equal(swapped, true); }
  finally { fs.lstat = original; }
}));

test('aggregate bytes and nodes cannot be evaded with many individually small files', () => fixture(async ({ dir, item, index }) => {
  const items = Array.from({ length: 3 }, (_, i) => ({ ...item, file: `${i.toString(16).padStart(64, '0')}.atif.json` }));
  await index(items);
  for (const value of [{ ...doc(), extra: { padding: 'x'.repeat(6 * 1024 * 1024) } }, { ...doc(), extra: { bulk: Array(35000).fill(null) } }]) {
    for (const entry of items) await writeFile(path.join(dir, entry.file), JSON.stringify(value));
    await assert.rejects(server.readRun(rel), /budget|limit/i);
  }
}));

test('discovery has independent count and aggregate byte budgets', () => fixture(async ({ root, item }) => {
  const { LIMITS } = await load('../lib/limits.ts');
  for (let i = 0; i < LIMITS.runs + 10; i++) {
    const dir = path.join(root, `many-${i}`, 'trajectories');
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, 'index.json'), JSON.stringify({ format: 'openttd-atif-index-v1', trajectories: [item] }));
  }
  assert.equal((await server.listRuns()).length, LIMITS.runs);
  await rm(root, { recursive: true });
  await mkdir(root);
  const largeIndex = JSON.stringify({ format: 'openttd-atif-index-v1', trajectories: [item], padding: 'x'.repeat(230 * 1024) });
  for (let i = 0; i < 25; i++) {
    const dir = path.join(root, `padded-${i}`, 'trajectories');
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, 'index.json'), largeIndex);
  }
  const runs = await server.listRuns();
  assert.ok(runs.length > 0 && runs.length < 25);
  assert.ok(runs.length * Buffer.byteLength(largeIndex) <= LIMITS.discoveryBytes);
}));

test('a configured root symlink is allowed, but final index links and directories are not', () => fixture(async ({ root, dir }) => {
  const link = `${root}-link`;
  await symlink(root, link);
  try {
    process.env.OUTPUTS_DIR = link;
    assert.equal(JSON.parse(await server.readRun(rel))[0].agent.name, 'synthetic');
    await rm(path.join(dir, 'index.json'));
    await symlink(path.join(dir, filename), path.join(dir, 'index.json'));
    await assert.rejects(server.readRun(rel));
    await rm(path.join(dir, 'index.json'));
    await mkdir(path.join(dir, 'index.json'));
    await assert.rejects(server.readRun(rel));
  } finally { await rm(link); }
}));

test('valid dropped files and lazy pages preserve copied context and observations', async () => {
  const value = doc();
  value.steps.unshift({ step_id: 1, source: 'system', message: 'copied', is_copied_context: true });
  value.steps[1].step_id = 2;
  const text = JSON.stringify(value);
  const run = await trace.parseAtifFile({ name: 'synthetic.json', size: Buffer.byteLength(text), text: async () => text });
  assert.equal(run.name, 'synthetic.json');
  assert.equal(trace.traceToMessages(value, false, 1)[0].node, 2);
  assert.equal(trace.traceToMessages(value, true, 1)[0].node, 1);
  const dangerous = { ...doc(), steps: [{ step_id: 1, source: 'agent', message: '', tool_calls: Array(129).fill({ tool_call_id: 'x', function_name: 'test', arguments: {} }) }] };
  assert.throws(() => trace.parseAtif(JSON.stringify(dangerous)), /limit|duplicate/);
  dangerous.steps[0].tool_calls.length = 2;
  assert.throws(() => trace.parseAtif(JSON.stringify(dangerous)), /duplicate/);
});

test('UTF-8 byte limits and parser errors do not echo private input', () => {
  assert.throws(() => trace.parseAtif('"' + '€'.repeat(6 * 1024 * 1024) + '"'), /byte limit/);
  assert.throws(() => trace.parseAtif('{ private-secret'), error => error.message === 'Invalid JSON');
});

test('manifest duplicate filenames are served and counted only once', () => fixture(async ({ item, index }) => {
  await index([item, item, { ...item, episode_id: 'different' }]);
  assert.equal(JSON.parse(await server.readRun(rel)).length, 1);
  const [run] = await server.listRuns();
  assert.equal(run.size, 1);
  assert.equal(run.episodes, 1);
}));

test('manifest entries are validated before use', () => fixture(async ({ item, index }) => {
  for (const invalid of [null, { ...item, size: -1 }, { ...item, size: '1' }, { ...item, episode_id: {} }, { ...item, file: '../escape' }]) {
    await index([invalid]);
    await assert.rejects(server.readRun(rel));
    assert.deepEqual(await server.listRuns(), []);
  }
}));

test('oversized manifests and trajectory files fail before reading/parsing', () => fixture(async ({ dir }) => {
  await writeFile(path.join(dir, filename), ' '.repeat(8 * 1024 * 1024 + 1));
  await assert.rejects(server.readRun(rel), /limit|large|budget/i);
  await writeFile(path.join(dir, 'index.json'), ' '.repeat(256 * 1024 + 1));
  await assert.rejects(server.readRun(rel), /limit|large|budget/i);
}));

test('manifest entry count is capped before deduplication', () => fixture(async ({ item, index }) => {
  await index(Array(257).fill(item));
  await assert.rejects(server.readRun(rel), /limit|large|budget/i);
}));

test('noncanonical paths and every symlink descendant are rejected', () => fixture(async ({ root, dir }) => {
  for (const invalid of ['run/../run/trajectories/index.json', './run/trajectories/index.json', 'run//trajectories/index.json', '/run/trajectories/index.json', 'run\\trajectories\\index.json'])
    await assert.rejects(server.readRun(invalid));
  await rm(path.join(dir, filename));
  await writeFile(path.join(root, 'outside.json'), JSON.stringify(doc()));
  await symlink(path.join(root, 'outside.json'), path.join(dir, filename));
  await assert.rejects(server.readRun(rel));
  await mkdir(path.join(root, 'linked-dir'));
  await symlink(dir, path.join(root, 'linked-dir/trajectories'));
  await assert.rejects(server.readRun('linked-dir/trajectories/index.json'));
}));

for (const mode of ['index-fifo', 'trajectory-fifo', 'index-swap-fifo', 'trajectory-swap-fifo', 'index-swap-symlink', 'trajectory-swap-symlink']) {
  test(`${mode} is rejected without hanging or reading escaped bytes`, () => {
    const result = spawnSync(process.execPath, ['--max-old-space-size=192', 'tests/unsafe-files.mjs', mode], {
      timeout: 4000, killSignal: 'SIGKILL', maxBuffer: 32 * 1024, encoding: 'utf8',
    });
    assert.equal(result.error, undefined, result.error?.message);
    assert.equal(result.status, 0, result.stderr?.slice(-1000));
  });
}

test('server validates JSON depth before recursive presentation', () => fixture(async ({ dir }) => {
  let value = doc();
  for (let i = 0; i < 70; i++) value = { ...doc(), subagent_trajectories: [value] };
  await writeFile(path.join(dir, filename), JSON.stringify(value));
  await assert.rejects(server.readRun(rel), /depth|limit|budget/i);
}));

test('client checks file.size before invoking text()', async () => {
  let read = false;
  await assert.rejects(trace.parseAtifFile({ name: 'large.json', size: 16 * 1024 * 1024 + 1,
    text: async () => { read = true; return JSON.stringify(doc()); } }), /limit|large|budget/i);
  assert.equal(read, false);
});

test('client byte, depth, node and trajectory budgets fail with bounded errors', () => {
  assert.throws(() => trace.parseAtif(' '.repeat(16 * 1024 * 1024 + 1)), /limit|large|budget/i);
  let value = doc();
  for (let i = 0; i < 70; i++) value = { ...doc(), subagent_trajectories: [value] };
  assert.throws(() => trace.parseAtif(JSON.stringify(value)), /depth|limit|budget/i);
  assert.throws(() => trace.parseAtif(JSON.stringify({ ...doc(), extra: { bulk: Array(100001).fill(null) } })), /node|limit|budget/i);
  assert.throws(() => trace.parseAtif(JSON.stringify(Array.from({ length: 257 }, doc))), /trajectory|limit|budget/i);
});

test('message limit stops conversion before touching hidden rows', () => {
  const value = doc();
  value.steps.push({ step_id: 2, source: 'agent', get message() { throw new Error('hidden row evaluated'); } });
  assert.equal(trace.traceToMessages(value, false, 1).length, 1);
});

test('host, origin, and browser fetch metadata enforce local same-origin access', async () => {
  const { isLocalRequest } = await load('../lib/local-request.ts');
  const req = (host = '127.0.0.1:3000', extras = {}) => new Request('http://127.0.0.1:3000/api/runs', { headers: { host, ...extras } });
  assert.equal(isLocalRequest(req()), true);
  assert.equal(isLocalRequest(req('localhost:3000', { origin: 'http://localhost:3000' })), true);
  assert.equal(isLocalRequest(req('[::1]:3000', { origin: 'http://[::1]:3000' })), true);
  for (const host of ['evil.test:3000', 'localhost.evil.test', '0.0.0.0:3000', '127.0.0.1.evil.test', '127.1:3000', '2130706433:3000', 'localhost:3000@evil.test', 'localhost:0', 'localhost:65536', 'localhost,evil.test'])
    assert.equal(isLocalRequest(req(host)), false, host);
  for (const origin of ['null', 'https://evil.test', 'http://localhost:3000', 'http://127.0.0.1:3001', 'http://127.0.0.1:3000/path', 'http://user@127.0.0.1:3000', 'http://127.0.0.1:3000?'])
    assert.equal(isLocalRequest(req('127.0.0.1:3000', { origin })), false, origin);
  assert.equal(isLocalRequest(req('127.0.0.1:3000', { 'sec-fetch-site': 'cross-site' })), false);
  assert.equal(isLocalRequest(req('127.0.0.1:3000', { 'sec-fetch-site': 'same-site' })), false);
  assert.equal(isLocalRequest(req('evil.test', { 'x-forwarded-host': 'localhost:3000' })), false);
  assert.equal(isLocalRequest(new Request('http://localhost/api/runs')), false);
});

test('launcher allows only loopback bind and rejects flag overrides', async () => {
  const { launchArgs } = await import('../scripts/local-next.mjs');
  assert.deepEqual(launchArgs('dev', []), ['dev', '--hostname', '127.0.0.1']);
  assert.deepEqual(launchArgs('start', ['-p', '3210', '--hostname=::1']), ['start', '--hostname', '::1', '--port', '3210']);
  for (const args of [['-H', '0.0.0.0'], ['--hostname=evil.test'], ['-H0.0.0.0'], ['--host', '0.0.0.0'], ['--hostname', 'localhost', '--hostname', '0.0.0.0'], ['--', '--hostname=0.0.0.0']])
    assert.throws(() => launchArgs('dev', args));
});
