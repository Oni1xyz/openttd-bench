import assert from 'node:assert/strict';
import { test } from 'node:test';
import { inventory, verifyVersion, verifyLock, CUTOFF } from '../scripts/verify-dependencies.mjs';
const pkg = { name: 'test', version: '1.0.0', resolved: 'https://registry.npmjs.org/test/-/test-1.0.0.tgz', integrity: `sha512-${Buffer.alloc(64).toString('base64')}` };
const metadata = (time = CUTOFF) => ({ time: { '1.0.0': time }, versions: { '1.0.0': { dist: { tarball: pkg.resolved, integrity: pkg.integrity } } } });
test('age gate fails closed on young/missing releases and mismatched artifacts', () => {
  assert.equal(verifyVersion(pkg, metadata()), CUTOFF);
  for (const time of ['2026-10-22T00:00:00.001Z', 'invalid', undefined]) {
    const data = metadata(); data.time['1.0.0'] = time;
    assert.throws(() => verifyVersion(pkg, data), /cutoff/);
  }
  const data = metadata(); data.versions['1.0.0'].dist.integrity = 'sha512-other';
  assert.throws(() => verifyVersion(pkg, data), /mismatch/);
});
test('full lock gate rejects incomplete and per-query advisory failures', async () => {
  const original = globalThis.fetch;
  const lock = { lockfileVersion: 3, packages: { '': {}, 'node_modules/test': pkg } };
  let osv = { results: [] };
  globalThis.fetch = async url => Response.json(url.includes('querybatch') ? osv : url.includes('advisories/bulk') ? {} : metadata());
  try {
    await assert.rejects(verifyLock(lock), /coverage/);
    osv = { results: [{ error: 'query failed' }] };
    await assert.rejects(verifyLock(lock), /coverage/);
    osv = { results: [{ next_page_token: 'more' }] };
    await assert.rejects(verifyLock(lock), /coverage/);
    osv = { results: [{}] };
    const result = await verifyLock(lock);
    assert.equal(result.registryVerified, 1);
    assert.equal(result.osvQueried, 1);
    assert.equal(result.npmQueried, 1);
  } finally { globalThis.fetch = original; }
});

test('full lock inventory counts optional/dev locations and deduplicates exact versions', () => {
  const lock = { lockfileVersion: 3, packages: { '': {}, 'node_modules/test': pkg,
    'node_modules/parent/node_modules/test': { ...pkg, dev: true, optional: true } } };
  const found = inventory(lock);
  assert.equal(found.locations, 2);
  assert.equal(found.pairs.length, 1);
  assert.throws(() => inventory({ ...lock, packages: { 'node_modules/test': { ...pkg, resolved: 'file:../outside' } } }));
  assert.throws(() => inventory({ ...lock, packages: { 'node_modules/test': { ...pkg, integrity: undefined } } }));
});
