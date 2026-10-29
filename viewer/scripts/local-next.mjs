import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

export function launchArgs(mode, args) {
  if (!['dev', 'start'].includes(mode)) throw new Error('Expected dev or start');
  let host = '127.0.0.1', port;
  const seen = new Set();
  for (let i = 0; i < args.length; i++) {
    const [flag, ...values] = args[i].split('=');
    const kind = ['-H', '--hostname'].includes(flag) ? 'host' : ['-p', '--port'].includes(flag) ? 'port' : null;
    if (!kind || seen.has(kind) || values.length > 1) throw new Error('Only one --hostname and --port option are supported');
    seen.add(kind);
    const value = values.length ? values[0] : args[++i];
    if (kind === 'host') {
      if (!['127.0.0.1', '::1', 'localhost'].includes(value)) throw new Error('Viewer must bind to loopback');
      // Avoid resolver-dependent localhost binding.
      host = value === 'localhost' ? '127.0.0.1' : value;
    } else {
      if (!/^[1-9][0-9]{0,4}$/.test(value ?? '') || Number(value) > 65535) throw new Error('Invalid port');
      port = value;
    }
  }
  return [mode, '--hostname', host, ...(port ? ['--port', port] : [])];
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const args = launchArgs(process.argv[2], process.argv.slice(3));
    const require = createRequire(import.meta.url);
    const child = spawn(process.execPath, [require.resolve('next/dist/bin/next'), ...args], {
      stdio: 'inherit', env: { ...process.env, HOST: '127.0.0.1', HOSTNAME: '127.0.0.1' },
    });
    for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
    child.on('error', () => { console.error('Unable to start viewer'); process.exitCode = 1; });
    child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
