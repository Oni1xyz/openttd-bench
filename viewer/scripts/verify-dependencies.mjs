// Read-only, fail-closed full-lock gate. Run in a disposable networked container.
// npm's --before assists resolution; registry timestamps independently enforce age.
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { gunzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';
export const CUTOFF = '2026-10-22T00:00:00Z';
const validIntegrity = value => typeof value === 'string' && /^sha512-[A-Za-z0-9+/]{86}==$/.test(value);

export function inventory(lock) {
  if (lock.lockfileVersion !== 3 || !lock.packages) throw new Error('Expected npm lockfile v3');
  const pairs = new Map();
  let locations = 0;
  for (const [location, pkg] of Object.entries(lock.packages)) {
    if (!location) continue;
    const name = pkg.name ?? location.split('node_modules/').at(-1);
    if (!location.includes('node_modules/') || pkg.link || !/^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(pkg.version ?? '') ||
        (!pkg.inBundle && (!pkg.resolved?.startsWith('https://registry.npmjs.org/') || !validIntegrity(pkg.integrity))))
      throw new Error(`Non-registry or unpinned dependency: ${location}`);
    locations++;
    const key = `${name}@${pkg.version}`;
    const previous = pairs.get(key);
    if (previous?.resolved && pkg.resolved && (previous.resolved !== pkg.resolved || previous.integrity !== pkg.integrity)) throw new Error(`Conflicting artifact: ${key}`);
    const entry = previous ?? { name, version: pkg.version, bundles: [] };
    if (pkg.resolved) Object.assign(entry, { resolved: pkg.resolved, integrity: pkg.integrity });
    if (pkg.inBundle) {
      let owner = location.slice(0, location.lastIndexOf('/node_modules/'));
      while (lock.packages[owner]?.inBundle) owner = owner.slice(0, owner.lastIndexOf('/node_modules/'));
      const parent = lock.packages[owner];
      if (!parent?.resolved?.startsWith('https://registry.npmjs.org/') || !validIntegrity(parent.integrity)) throw new Error(`Unpinned bundle: ${location}`);
      entry.bundles.push({ owner, resolved: parent.resolved, integrity: parent.integrity,
        member: `package/${location.slice(owner.length + 1)}/package.json` });
    }
    pairs.set(key, entry);
  }
  if (!locations) throw new Error('Empty lock');
  return { locations, pairs: [...pairs.values()] };
}
export function verifyVersion(pkg, metadata) {
  const version = metadata.versions?.[pkg.version];
  const published = metadata.time?.[pkg.version];
  if (!published || !Number.isFinite(Date.parse(published)) || Date.parse(published) > Date.parse(CUTOFF))
    throw new Error(`Release cutoff failed: ${pkg.name}@${pkg.version} (${published ?? 'missing'})`);
  if (!version || !validIntegrity(version.dist?.integrity) || (pkg.resolved && (version.dist?.tarball !== pkg.resolved || version.dist?.integrity !== pkg.integrity)))
    throw new Error(`Registry artifact mismatch: ${pkg.name}@${pkg.version}`);
  return published;
}
async function json(url, body) {
  for (let attempt = 0; ; attempt++) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(60_000),
        ...(body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : {}) });
      if (!response.ok) throw new Error(`HTTP ${response.status} from ${new URL(url).hostname}`);
      return await response.json();
    } catch (error) {
      if (attempt >= 2) throw error;
      await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
    }
  }
}
export async function verifyLock(lock) {
  const { locations, pairs } = inventory(lock);
  const byName = new Map();
  for (const pkg of pairs) {
    if (!byName.has(pkg.name)) byName.set(pkg.name, []);
    byName.get(pkg.name).push(pkg);
  }
  const queue = [...byName];
  const ages = [];
  await Promise.all(Array.from({ length: 4 }, async () => {
    while (queue.length) {
      const [name, versions] = queue.pop();
      const metadata = await json(`https://registry.npmjs.org/${encodeURIComponent(name)}`);
      for (const pkg of versions) ages.push({ name, version: pkg.version, published: verifyVersion(pkg, metadata) });
    }
  }));
  // Bundled packages have no separate tarball/integrity in an npm lock. Verify
  // their exact package manifests inside the hash-checked containing artifact.
  const bundleGroups = new Map();
  for (const pkg of pairs) for (const bundle of pkg.bundles) {
    if (!bundleGroups.has(bundle.resolved)) bundleGroups.set(bundle.resolved, []);
    bundleGroups.get(bundle.resolved).push({ ...bundle, name: pkg.name, version: pkg.version });
  }
  let bundledVerified = 0;
  for (const [url, members] of bundleGroups) {
    const response = await fetch(url, { signal: AbortSignal.timeout(60_000) });
    if (!response.ok) throw new Error('Bundle download failed');
    const compressed = Buffer.from(await response.arrayBuffer());
    if (`sha512-${createHash('sha512').update(compressed).digest('base64')}` !== members[0].integrity) throw new Error('Bundle integrity mismatch');
    const tar = gunzipSync(compressed, { maxOutputLength: 128 * 1024 * 1024 });
    const wanted = new Map(members.map(m => [m.member, m]));
    for (let offset = 0; offset + 512 <= tar.length; ) {
      const header = tar.subarray(offset, offset + 512);
      const field = (start, length) => header.subarray(start, start + length).toString().split('\0')[0];
      const name = [field(345, 155), field(0, 100)].filter(Boolean).join('/');
      const sizeText = field(124, 12).trim();
      if (!name && !sizeText) break;
      if (!/^[0-7]+$/.test(sizeText)) throw new Error('Unsupported bundle tar header');
      const size = Number.parseInt(sizeText, 8);
      if (offset + 512 + size > tar.length) throw new Error('Truncated bundle');
      if (wanted.has(name)) {
        if (![0, 48].includes(header[156]) || size > 1024 * 1024) throw new Error('Invalid bundled manifest');
        const manifest = JSON.parse(tar.subarray(offset + 512, offset + 512 + size).toString());
        const expected = wanted.get(name);
        if (manifest.name !== expected.name || manifest.version !== expected.version) throw new Error('Bundled version mismatch');
        wanted.delete(name);
        bundledVerified++;
      }
      offset += 512 + Math.ceil(size / 512) * 512;
    }
    if (wanted.size) throw new Error('Missing bundled manifests');
  }
  const osvMatches = [];
  let osvQueried = 0;
  for (let i = 0; i < pairs.length; i += 100) {
    const batch = pairs.slice(i, i + 100);
    const data = await json('https://api.osv.dev/v1/querybatch', {
      queries: batch.map(pkg => ({ package: { ecosystem: 'npm', name: pkg.name }, version: pkg.version })),
    });
    if (!Array.isArray(data.results) || data.results.length !== batch.length) throw new Error('Incomplete OSV coverage');
    for (const [j, result] of data.results.entries()) {
      if (!result || typeof result !== 'object' || Array.isArray(result) || result.error ||
          (result.vulns !== undefined && (!Array.isArray(result.vulns) || result.vulns.some(v => typeof v?.id !== 'string'))))
        throw new Error('Invalid OSV result; coverage incomplete');
      if (result.next_page_token) throw new Error('OSV pagination requires review; coverage incomplete');
      osvQueried++;
      if (result.vulns?.length) osvMatches.push({ name: batch[j].name, version: batch[j].version, ids: result.vulns.map(v => v.id) });
    }
  }
  const npm = await json('https://registry.npmjs.org/-/npm/v1/security/advisories/bulk',
    Object.fromEntries([...byName].map(([name, versions]) => [name, versions.map(pkg => pkg.version)])));
  if (!npm || typeof npm !== 'object' || Array.isArray(npm) || npm.error) throw new Error('Invalid npm advisory response');
  const npmAdvisories = Object.entries(npm).flatMap(([name, entries]) => {
    if (!byName.has(name) || !Array.isArray(entries)) throw new Error('Unexpected npm advisory response');
    return entries.map(a => ({ name, id: a.id, url: a.url, severity: a.severity, range: a.vulnerable_versions }));
  });
  if (ages.length !== pairs.length || osvQueried !== pairs.length) throw new Error('Incomplete lock verification');
  return { cutoff: CUTOFF, locations, uniquePairs: pairs.length, uniqueNames: byName.size,
    registryVerified: ages.length, bundledVerified, osvQueried, npmQueried: pairs.length, osvMatches, npmAdvisories,
    releases: ages.sort((a, b) => `${a.name}@${a.version}`.localeCompare(`${b.name}@${b.version}`)) };
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const result = await verifyLock(JSON.parse(await readFile('package-lock.json', 'utf8')));
    console.log(JSON.stringify(result, null, 2));
    if (result.osvMatches.length || result.npmAdvisories.length) process.exitCode = 1;
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
