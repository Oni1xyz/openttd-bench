"""Real unprivileged scripted seat for smoke_game.py; no model calls."""
import json
import os
from pathlib import Path
import socket
import urllib.error
import urllib.request

assert os.environ.get('OPENTTD_ISOLATED_SMOKE') == '1', 'container smoke only'
assert os.getuid() != 0
for path in ('/opt/openttd/seats.json', '/home/agent2/.smoke-private'):
    try:
        Path(path).read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('cross-UID private file readable: ' + path)
# Ordinary procfs remains visible; no secret values may ride command arguments.
for proc in Path('/proc').iterdir():
    if proc.name.isdigit():
        try:
            argv = (proc / 'cmdline').read_bytes()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        assert b'smoke-admin-' not in argv, 'admin credential in visible argv'

from openttd_client import Game, OpenTTDError
# Read only this seat's root-created, owner-only credentials, not shell source.
settings = dict(line.split('=', 1) for line in (Path.home() / '.openttd_seat').read_text().splitlines() if line)
game = Game(settings['OPENTTD_API_URL'], settings['OPENTTD_API_TOKEN'])
initial = game.state()
assert initial['seat']['company'] == 0
for path, body, expected in [('/api/admin/snapshot', None, 401),
                             ('/api/batch', {'items': [None] * 101}, 400),
                             ('/api/batch', {'padding': 'x' * (1024 * 1024)}, 413)]:
    try:
        game.transport.request('GET' if body is None else 'POST', path, body)
    except OpenTTDError as exc:
        assert exc.status == expected, (path, exc.status)
    else:
        raise AssertionError('unsafe API request accepted: ' + path)
towns = game.inspect.towns(limit=20)
town_rows = towns.get('towns', []) if isinstance(towns, dict) else towns
assert len(town_rows) >= 2, towns
built = None
for x, y in ((40, 40), (50, 50), (60, 60), (70, 70), (80, 80)):
    try:
        game.land.demolish(x=x, y=y)
        result = game.rail.depot(x=x, y=y, front_x=x + 1, front_y=y)
        if result.ok:
            built = (x, y)
            break
    except OpenTTDError:
        continue
assert built is not None, 'scripted depot construction failed'
# Requery actual game state rather than accepting only the command receipt.
tile = game.inspect.tile(x=built[0], y=built[1])
assert tile.get('owner') == 0, tile
wait = game.wait(days=2, timeout=90)
assert wait.get('status') != 'blocked', wait
later = game.state()
assert later['date']['date'] > initial['date']['date'], (initial['date'], later['date'])
retired = game.retire()
print(json.dumps({'uid': os.getuid(), 'seat': 0, 'towns_visible': len(town_rows),
                  'depot': list(built), 'tile_owner': tile['owner'], 'wait_status': wait['status'],
                  'initial_date': initial['date'], 'later_date': later['date'],
                  'private_file_denials': 2, 'http_denials': 3,
                  'retirement': retired}, sort_keys=True))
