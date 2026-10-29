// Run only as a bounded child process: a blocking-open regression must not hang the suite.
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { load } from './load-ts.mjs';
const { readRun, listRuns } = await load('../lib/server-runs.ts');
const mode = process.argv[2];
const root = await fs.mkdtemp(path.join(tmpdir(), 'viewer-special-'));
process.env.OUTPUTS_DIR = root;
const dir = path.join(root, 'run/trajectories');
const file = `${'c'.repeat(64)}.atif.json`;
await fs.mkdir(dir, { recursive: true });
await fs.writeFile(path.join(dir, 'index.json'), JSON.stringify({ format: 'openttd-atif-index-v1', trajectories: [{ file, episode_id: 'e', size: 1 }] }));
await fs.writeFile(path.join(dir, file), JSON.stringify({ schema_version: 'ATIF-v1.8', agent: { name: 'canary', version: '1' }, steps: [{ step_id: 1, source: 'agent', message: 'private' }] }));
const target = path.join(dir, mode.startsWith('index') ? 'index.json' : file);
const outside = path.join(root, 'escaped.json');
await fs.copyFile(target, outside);
const replace = async () => {
  await fs.unlink(target);
  if (mode.endsWith('symlink')) await fs.symlink(outside, target);
  else execFileSync('mkfifo', [target]);
};
let swapped = false;
if (mode.includes('swap')) {
  const original = fs.open;
  fs.open = async (name, ...args) => {
    if (name === target && !swapped) { swapped = true; await replace(); }
    return original(name, ...args);
  };
} else await replace();
try {
  await assert.rejects(readRun('run/trajectories/index.json'));
  if (mode.startsWith('index')) assert.deepEqual(await listRuns(), []);
  if (mode.includes('swap')) assert.equal(swapped, true, 'secure reader must exercise descriptor open');
} finally {
  await fs.rm(root, { recursive: true, force: true });
}
