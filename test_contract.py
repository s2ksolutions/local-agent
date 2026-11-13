"""Functional tests for the task contract, evidence ledger, completion gate and write policy."""
import asyncio
import re
import shutil
import sys
import tempfile

from runtime import Runtime
from store import Store

class FakeBrowser:
    def set_event_sink(self, sink):
        pass

class StubModel:
    def __init__(self, handler):
        self.handler = handler
        self.requests = 0

    async def __call__(self, model, payload, embed=False, r=None, key=None):
        self.requests += 1
        return {'message': self.handler(self.requests, payload)}

def tool_call(name, arguments):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': name, 'arguments': arguments}}]}

def final(text):
    return {'role': 'assistant', 'content': text}

def make_runtime(tmp, settings=None):
    s = Store(tmp)
    s.put('setting', 'runtime', {'endpoint': 'http://127.0.0.1:11434', 'loadTimeout': 30, 'stallTimeout': 30, **(settings or {})})
    s.put('workspace', 'ws1', {'name': 'Test', 'defaultAgent': 'a1', 'timezone': 'America/New_York', 'paused': False, 'instructions': ''})
    s.put('agent', 'a1', {'name': 'Tester', 'modelConfig': {'model': 'stub', 'allowed': ['stub'], 'context': 4096, 'output': 512, 'temperature': 0.1, 'vision': False}, 'capabilities': ['files', 'workspaces', 'agents', 'activity'], 'write': True, 'browser': False, 'memoryRead': [], 'memoryWrite': 'disabled', 'promptIds': [], 'defaultPrompt': '', 'allowWorkflow': False})
    return s, Runtime(s, FakeBrowser())

async def drive(runtime, run_id, timeout=15):
    task = runtime.tasks.get(run_id)
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout)

def latest_evidence(payload):
    ids = re.findall(r'"evidence_id":\s*"(ev-\d+)"', str(payload.get('messages')))
    return ids[-1] if ids else 'ev-0'

async def scenario_verified(tmp):
    """Contract proposed, both requirements verified with recorded evidence -> completed_verified."""
    s, runtime = make_runtime(tmp)
    script = [
        tool_call('submit_contract', {'requirements': [{'description': 'Reproduce the failure'}, {'description': 'Repair and confirm'}]}),
        tool_call('list_files', {}),
        None,  # filled per-request: verify r1 with the latest evidence id
        None,
        final('Everything passed.'),
    ]

    def handler(n, payload):
        if n == 3:
            return tool_call('verify_requirement', {'requirement': 'r1', 'evidence': [latest_evidence(payload)]})
        if n == 4:
            return tool_call('verify_requirement', {'requirement': 'r2', 'evidence': [latest_evidence(payload)]})
        return script[n - 1]

    runtime.model_call = StubModel(handler)
    run = runtime.submit('ws1', 'a1', 'Fix and verify')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed_verified', (stored['status'], stored.get('error'))
    contract = stored['contract']
    assert all(r['state'] == 'satisfied' for r in contract['requirements']), contract
    assert all(r['evidence_refs'] for r in contract['requirements']), contract
    assert stored['evidence']['ev-1']['tool'] == 'submit_contract'
    assert stored['evidence']['ev-2']['tool'] == 'list_files'
    print('scenario verified: OK  status=', stored['status'])

async def scenario_unverified(tmp):
    """One requirement left without evidence -> completed_unverified with an explicit warning."""
    s, runtime = make_runtime(tmp)
    runtime.model_call = StubModel(lambda n, payload: [
        tool_call('submit_contract', {'requirements': [{'description': 'Do A'}, {'description': 'Do B'}]}),
        tool_call('list_files', {}),
        tool_call('verify_requirement', {'requirement': 'r1', 'evidence': ['ev-1']}),
        final('Everything passed.'),  # claim without evidence for r2
    ][n - 1])
    run = runtime.submit('ws1', 'a1', 'Fix and verify')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed_unverified', stored['status']
    assert any('Unverified requirements' in w for w in stored.get('warnings', [])), stored.get('warnings')
    assert 'r2 Do B' in stored['warnings'][-1]
    reply = s.get('message', 'reply-' + run['id'])
    assert reply.get('warnings'), 'reply must carry the warning'
    print('scenario unverified: OK  status=', stored['status'])

async def scenario_bogus_evidence(tmp):
    """verify_requirement with an unrecorded evidence id is rejected as a tool failure."""
    s, runtime = make_runtime(tmp)
    runtime.model_call = StubModel(lambda n, payload: [
        tool_call('submit_contract', {'requirements': [{'description': 'Do A'}]}),
        tool_call('verify_requirement', {'requirement': 'r1', 'evidence': ['ev-99']}),
        final('Trust me, it works.'),
    ][n - 1])
    run = runtime.submit('ws1', 'a1', 'Verify')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed_unverified', stored['status']
    failed = stored['states']['chat'].get('failed_tools', [])
    assert failed and 'Evidence not recorded' in failed[0]['error'], failed
    print('scenario bogus evidence: OK  error=', failed[0]['error'][:60])

async def scenario_no_removal(tmp):
    """A second submit_contract cannot remove or weaken existing requirements."""
    s, runtime = make_runtime(tmp)
    runtime.model_call = StubModel(lambda n, payload: [
        tool_call('submit_contract', {'requirements': [{'description': 'Keep me'}, {'description': 'Me too'}]}),
        tool_call('submit_contract', {'requirements': [{'description': 'Keep me'}]}),  # attempt to drop the second
        final('Done.'),
    ][n - 1])
    run = runtime.submit('ws1', 'a1', 'Contract test')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    contract = stored['contract']
    assert len(contract['requirements']) == 2, contract
    assert contract['revision'] == 2
    assert all(r['state'] == 'pending' for r in contract['requirements'])
    print('scenario no-removal: OK  requirements=', [r['id'] for r in contract['requirements']])

async def scenario_write_policy(tmp):
    """Section-scoped, denied, JSON-validity and exact-byte write rules are enforced before disk."""
    s, runtime = make_runtime(tmp)
    root = s.project('ws1')
    (root / 'app.html').write_text('<harness>fixed harness</harness>\n<!-- APP START -->\nold code\n<!-- APP END -->\n', encoding='utf-8')
    (root / 'harness.js').write_text('console.log(1)\n', encoding='utf-8')
    (root / 'profile.json').write_text('{"name": "old"}', encoding='utf-8')
    policy = {'files': {
        'app.html': {'mode': 'sections', 'markers': [('<!-- APP START -->', '<!-- APP END -->')]},
        'harness.js': {'mode': 'deny'},
        'profile.json': {'validJson': True, 'noTrailingNewline': True},
    }}
    run = {'workspace': 'ws1', 'id': 'policy-test', 'write_policy': policy, 'states': {}}
    agent = s.get('agent', 'a1')

    async def write(path, content):
        return await runtime.execute_tool(run, agent, 'write_file', {'path': path, 'content': content})

    # Edit inside the permitted section: accepted.
    await write('app.html', '<harness>fixed harness</harness>\n<!-- APP START -->\nnew code\n<!-- APP END -->\n')
    assert 'new code' in (root / 'app.html').read_text(encoding='utf-8')
    # Edit outside the permitted section (harness changed): rejected, disk untouched.
    try:
        await write('app.html', '<harness>TAMPERED</harness>\n<!-- APP START -->\nnew code\n<!-- APP END -->\n')
        raise AssertionError('harness edit should have been rejected')
    except ValueError as error:
        assert 'outside the permitted sections' in str(error)
    assert 'fixed harness' in (root / 'app.html').read_text(encoding='utf-8')
    # Removing a marker section: rejected.
    try:
        await write('app.html', 'no markers at all')
        raise AssertionError('marker removal should have been rejected')
    except ValueError as error:
        assert 'marker' in str(error)
    # Denied file: rejected outright.
    try:
        await write('harness.js', 'console.log(2)\n')
        raise AssertionError('denied write should have been rejected')
    except ValueError as error:
        assert 'protected' in str(error)
    assert (root / 'harness.js').read_text(encoding='utf-8') == 'console.log(1)\n'
    # Invalid JSON: rejected.
    try:
        await write('profile.json', '{"name": broken')
        raise AssertionError('invalid JSON should have been rejected')
    except ValueError as error:
        assert 'valid JSON' in str(error)
    # Valid JSON but trailing newline: rejected (exact-byte rule).
    try:
        await write('profile.json', '{"name": "new"}\n')
        raise AssertionError('trailing newline should have been rejected')
    except ValueError as error:
        assert 'trailing newline' in str(error)
    # Valid JSON without trailing newline: accepted.
    await write('profile.json', '{"name": "new"}')
    assert (root / 'profile.json').read_text(encoding='utf-8') == '{"name": "new"}'
    print('scenario write policy: OK  (sections, deny, validJson, noTrailingNewline)')

async def main():
    base = tempfile.mkdtemp(prefix='contract-test-')
    try:
        for i, scenario in enumerate([scenario_verified, scenario_unverified, scenario_bogus_evidence, scenario_no_removal, scenario_write_policy], 1):
            await scenario(base + '/s' + str(i))
        print('ALL CONTRACT SCENARIOS PASSED')
    finally:
        shutil.rmtree(base, ignore_errors=True)

asyncio.run(main())
