from pathlib import Path
import builtins
import io
import tarfile
from types import SimpleNamespace

import pytest
import fetch_content
import init_game


def test_missing_game_assets_fail_offline(tmp_path, monkeypatch):
    paths = SimpleNamespace(root=tmp_path, cache=tmp_path / 'cache', install=tmp_path / 'install')
    calls = []
    monkeypatch.setattr(fetch_content, 'install', lambda *a: calls.append(a))
    with pytest.raises(RuntimeError, match='prebuilt'):
        init_game.acquire_binaries(paths)
    assert not calls


def test_missing_python_deps_do_not_install(monkeypatch):
    real_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'fastapi':
            raise ImportError('simulated missing package')
        return real_import(name, *args, **kwargs)
    calls = []
    monkeypatch.setattr(builtins, '__import__', unavailable)
    monkeypatch.setattr(init_game.subprocess, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match='prebuilt'):
        init_game.ensure_python_deps()
    assert calls == []


def test_missing_system_deps_do_not_install(monkeypatch):
    calls = []
    monkeypatch.setattr(init_game, '_lib_present', lambda name: False)
    monkeypatch.setattr(init_game.subprocess, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match='prebuilt'):
        init_game.ensure_system_deps(SimpleNamespace(dev=False))
    assert calls == []


def test_game_archive_cannot_escape_install_root(tmp_path, monkeypatch):
    archive = tmp_path / 'hostile.tar.xz'
    with tarfile.open(archive, 'w:xz') as tar:
        info = tarfile.TarInfo('bundle/../escaped')
        info.size = 6
        tar.addfile(info, io.BytesIO(b'canary'))
    monkeypatch.setattr(fetch_content, 'fetch', lambda *a, **k: {'openttd': archive})
    with pytest.raises(tarfile.FilterError):
        fetch_content.install(tmp_path / 'cache', tmp_path / 'install')
    assert not (tmp_path / 'escaped').exists()
