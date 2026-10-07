"""Exercise real bridge transitions without starting QQ or a control server."""
import subprocess
from pathlib import Path


def test_room_update_does_not_end_connected_capture_but_destruction_does(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'bridge/napcat-plugin/index.mjs'
    module = tmp_path / 'bridge.mjs'
    module.write_text(source.read_text() + '\nexport { recordEvent, state };\n')
    script = '''
import assert from 'node:assert/strict';
import { recordEvent, state } from './bridge.mjs';
state.call = { phase: 'connected', inviteAt: 'same-invite', endedAt: null, endReason: null };
recordEvent('onS2CActionToAVSDK', [{ destroyReason: 0, pushRoomInfoList: [{ roomId: 'room' }] }, 19]);
assert.equal(state.call.phase, 'connected');
assert.equal(state.call.endedAt, null);
assert.equal(state.call.inviteAt, 'same-invite');
recordEvent('onS2CActionToAVSDK', [{ pushRoomInfoList: [] }, 19]);
assert.equal(state.call.phase, 'connected');
recordEvent('onS2CActionToAVSDK', [{ destroyReason: 1 }, 14]);
assert.equal(state.call.phase, 'ended');
assert.equal(state.call.endReason, 1);
assert.ok(state.call.endedAt);
console.log('PASS: room update retains capture, actual destruction ends call');
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
