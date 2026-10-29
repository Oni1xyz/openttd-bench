from scripts import watch_run
import pytest


def test_notifications_are_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(watch_run.urllib.request, 'urlopen', lambda *a, **k: calls.append(a))
    watch_run.post('', 'synthetic title', 'synthetic body')
    watch_run.post(None, 'synthetic title', 'synthetic body')
    assert calls == []


def test_topic_cannot_change_request_destination(monkeypatch):
    calls = []
    monkeypatch.setattr(watch_run.urllib.request, 'urlopen', lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        watch_run.post('../other?query=1', 'synthetic title', 'synthetic body')
    assert calls == []
