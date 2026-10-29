import { readFile } from 'node:fs/promises';
import { stripTypeScriptTypes } from 'node:module';

const modules = new Map();
async function moduleURL(url) {
  if (modules.has(url.href)) return modules.get(url.href);
  let source = stripTypeScriptTypes(await readFile(url, 'utf8'));
  for (const match of [...source.matchAll(/from\s+["'](\.[^"']+)["']/g)]) {
    const spec = match[1].endsWith('.ts') ? match[1] : `${match[1]}.ts`;
    source = source.replace(match[0], `from ${JSON.stringify(await moduleURL(new URL(spec, url)))}`);
  }
  const result = `data:text/javascript;base64,${Buffer.from(`${source}\n//# sourceURL=${url.href}`).toString('base64')}`;
  modules.set(url.href, result);
  return result;
}
export async function load(file) {
  return import(await moduleURL(new URL(file, import.meta.url)));
}
