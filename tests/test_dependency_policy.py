"""Offline fixtures for the metadata-only supply-chain gate."""
import base64
import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scripts import check_dependencies as checker

CUTOFF = datetime(2026, 10, 22, tzinfo=timezone.utc)


def test_seven_day_cutoff_and_artifact_age():
    checker.check_age('2026-10-21T23:59:59Z', CUTOFF)
    with pytest.raises(ValueError):
        checker.check_age('2026-10-22T00:00:01Z', CUTOFF)


def test_missing_baked_agent_lock_is_an_error(tmp_path):
    (tmp_path / 'uv.lock').write_text('package = []\n')
    with pytest.raises(ValueError, match='missing npm lock'):
        checker.inventory(tmp_path, True)


def test_python_artifact_hash_yank_and_age_are_checked(monkeypatch):
    url = 'https://files.pythonhosted.org/synthetic.whl'
    artifact = {'url': url, 'digests': {'sha256': 'a' * 64},
                'upload_time_iso_8601': '2026-10-01T00:00:00Z', 'yanked': False}
    monkeypatch.setattr(checker, 'get_json', lambda *a, **k: {'urls': [artifact]})
    record = {'key': ('PyPI', 'synthetic-test-only', '1.0'),
              'artifacts': [{'url': url, 'hash': 'sha256:' + 'a' * 64}]}
    assert checker.verify_record(record, CUTOFF)['artifacts'] == 1
    for field, value in [('yanked', True), ('upload_time_iso_8601', '2026-10-25T00:00:00Z')]:
        prior = artifact[field]
        artifact[field] = value
        with pytest.raises(ValueError):
            checker.verify_record(record, CUTOFF)
        artifact[field] = prior
    record['artifacts'][0]['hash'] = 'sha256:' + 'b' * 64
    with pytest.raises(ValueError):
        checker.verify_record(record, CUTOFF)


def test_npm_invalid_sri_is_rejected_even_when_registry_matches():
    sri = 'sha512-' + base64.b64encode(b'x' * 64).decode()
    url = 'https://registry.npmjs.org/synthetic/-/synthetic-1.0.0.tgz'
    record = {'key': ('npm', 'synthetic', '1.0.0'), 'artifacts': [{'resolved': url, 'integrity': sri}]}
    meta = {'time': {'1.0.0': '2026-10-01T00:00:00Z'},
            'versions': {'1.0.0': {'dist': {'tarball': url, 'integrity': sri}}}}
    assert checker.verify_record(record, CUTOFF, meta)['version'] == '1.0.0'
    record['artifacts'][0]['integrity'] = sri + '='
    meta['versions']['1.0.0']['dist']['integrity'] = sri + '='
    with pytest.raises(ValueError):
        checker.verify_record(record, CUTOFF, meta)


def test_osv_incomplete_coverage_never_reports_success(tmp_path, monkeypatch):
    (tmp_path / 'pyproject.toml').write_text('[tool.uv]\nexclude-newer="2026-10-22T00:00:00Z"\n')
    monkeypatch.setattr(checker, 'inventory', lambda *a, **k: [{'key': ('PyPI', 'synthetic', '1')}])
    monkeypatch.setattr(checker, 'get_json', lambda *a, **k: {'results': []})
    with pytest.raises(ValueError, match='incomplete coverage'):
        checker.audit(tmp_path, python_only=True, metadata=False)


def test_exported_requirements_are_hash_locked_to_reviewed_graph():
    import re
    import tomllib
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / 'uv.lock').read_text())
    expected = {}
    for package in lock['package']:
        if 'registry' not in package['source']:
            continue
        artifacts = [*package.get('wheels', []), *([package['sdist']] if package.get('sdist') else [])]
        expected[(package['name'], package['version'])] = {a['hash'].split(':', 1)[1] for a in artifacts}
    for file in ('image/test-requirements.lock', 'image/requirements.lock'):
        seen = set()
        text = (root / file).read_text().replace('\\\n', '')
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.match(r'^([a-zA-Z0-9_.-]+)==([^\s;]+)', line)
            assert match, f'unpinned requirement in {file}'
            key = match.groups()
            hashes = set(re.findall(r'--hash=sha256:([0-9a-f]{64})', line))
            assert key in expected and hashes == expected[key], key
            seen.add(key)
        if file.endswith('test-requirements.lock'):
            assert seen == set(expected), 'test image does not cover the exact locked graph'
        else:
            assert {'fastapi', 'uvicorn', 'pydantic'} <= {name for name, _ in seen}
