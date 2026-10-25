import json
import os
from test_account import server


def test_oauth_write_does_not_follow_predictable_temp_symlink(server, tmp_path):
    victim = tmp_path / 'unrelated'
    victim.write_text('unchanged')
    predictable = server.OAUTH.with_suffix('.tmp')
    predictable.symlink_to(victim)
    server._private_json(server.OAUTH, {'access_token': 'fixture-only'})
    assert victim.read_text() == 'unchanged'
    assert predictable.is_symlink()
    assert json.loads(server.OAUTH.read_text()) == {'access_token': 'fixture-only'}
    assert os.stat(server.OAUTH).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.oauth-*'))
