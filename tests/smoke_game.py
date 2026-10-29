#!/usr/bin/env python3
"""Real offline OpenTTD -> unprivileged scripted seat -> horizon -> grader smoke.

Run ONLY via scripts/smoke-isolated.sh. No model/provider authentication or inference.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request


def api(url, token, method, path, body=None, timeout=30):
    request = urllib.request.Request(url + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def main():
    assert os.environ.get('OPENTTD_ISOLATED_SMOKE') == '1' and os.geteuid() == 0, 'isolated container root required'
    assert Path('/.dockerenv').exists(), 'Docker smoke only'
    from openttd_bench.tasks.free_for_all import spec as scenario
    from openttd_bench.task_spec import TaskSpec
    from openttd_bench.payload import build_payload, extract_payload
    from openttd_bench.grader import score, metrics
    started = time.monotonic()
    spec = scenario(101)
    # A test-only one-year horizon; production scenarios/scoring remain untouched.
    spec.rules.duration_years = 1
    spec = TaskSpec.model_validate(spec.model_dump())
    seats = {'tokens': {secrets.token_hex(24): slot for slot in range(spec.llm_seats)},
             'admin': 'smoke-admin-' + secrets.token_hex(24)}
    root = Path('/opt/openttd')
    extract_payload(build_payload(spec, seats), root)
    other = pwd.getpwnam('agent2')
    canary = Path(other.pw_dir) / '.smoke-private'
    canary.write_text('synthetic peer data\n')
    canary.chmod(0o600)
    os.chown(canary, other.pw_uid, other.pw_gid)
    url = f'http://127.0.0.1:{spec.ports.api}'
    log_path = Path('/work/init-game.log')
    with log_path.open('wb') as log:
        proc = subprocess.Popen([sys.executable, str(root / 'init_game.py'), '--root', str(root),
                                 '--world', str(root / 'world.json'), '--seats', str(root / 'seats.json')],
                                stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f'init_game exited {proc.returncode}')
            try:
                health = api(url, '', 'GET', '/health', timeout=2)
                if health.get('boot_complete'):
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.5)
        else:
            raise TimeoutError('game readiness deadline exceeded')
        # Other scripted seats retire immediately; they are not fake model runs.
        for slot in range(1, spec.llm_seats):
            api(url, seats['admin'], 'POST', '/api/admin/retire', {'company': slot})
        agent = pwd.getpwnam('agent1')
        env = {'PATH': os.environ['PATH'], 'HOME': agent.pw_dir,
               'OPENTTD_ISOLATED_SMOKE': '1', 'TMPDIR': agent.pw_dir}
        child = subprocess.run(['setpriv', '--reuid=agent1', '--regid=agent1', '--init-groups', '--no-new-privs',
                                sys.executable, '-I', '/source/tests/smoke_seat.py'],
                               env=env, capture_output=True, text=True, timeout=140)
        if child.returncode:
            raise RuntimeError('scripted seat failed: ' + child.stderr[-6000:] + child.stdout[-2000:])
        seat_result = json.loads(child.stdout)
        run_out = api(url, seats['admin'], 'POST', '/api/admin/run_out', {'timeout_seconds': 300}, timeout=330)
        final = api(url, seats['admin'], 'GET', '/api/admin/snapshot', timeout=90)
        assert final['date']['year'] >= spec.horizon_year, final['date']
        assert final['accounting_snapshot']['fresh'], final['accounting_snapshot']
        assert '0' in final['companies'], 'missing scripted company snapshot'
        outcome = score(spec, final, 0)
        measured = metrics(spec, final, 0, outcome)
        assert measured['horizon_reached'] == 1
        versions = {name: subprocess.check_output(argv, text=True, timeout=10).strip() for name, argv in
                    [('node', ['node', '--version']), ('codex', ['codex', '--version'])]}
        report = {'ok': True, 'scenario': spec.name, 'test_horizon_years': 1,
                  'no_model_calls': True, 'elapsed_seconds': round(time.monotonic() - started, 2),
                  'seat': seat_result, 'run_out': run_out, 'metrics': measured, 'tools': versions}
        print(json.dumps(report, indent=2, sort_keys=True))
    except BaseException:
        print(log_path.read_text(errors='replace')[-10000:], file=sys.stderr)
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == '__main__':
    main()
