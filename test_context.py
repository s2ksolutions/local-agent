"""Functional tests for context compaction, the elapsed-time budget and contract correction."""
import asyncio
import shutil
import sys
import tempfile

from runtime import Runtime
from store import Store

class FakeBrowser:
    def set_event_sink(self, sink):
        pass

class StubModel:
    """Scripted model; each entry is a response message or a callable(n, payload) -> message."""
    def __init__(self, script, delays=None):
        self.script = script
        self.delays = delays or {}
        self.requests = 0
        self.payload_sizes = []
        self.saw_compaction_note = False

    async def __call__(self, model, payload, embed=False, r=None, key=None):
        if self.requests in self.delays:
            await asyncio.sleep(self.delays[self.requests])
        self.requests += 1
        total = len(str(payload.get('messages', [])))
        self.payload_sizes.append(total)
        if '[context compacted]' in str(payload.get('messages', [])):
            self.saw_compaction_note = True
        entry = self.script[self.requests - 1] if self.requests - 1 < len(self.script) else self.script[-1]
        message = entry(self.requests, payload) if callable(entry) else entry
        return {'message': message}

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

async def drive(runtime, run_id, timeout=20):
    task = runtime.tasks.get(run_id)
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout)

async def scenario_compaction(tmp):
    """Long tool histories are compacted; evidence IDs and the protocol survive; the run completes."""
    s, runtime = make_runtime(tmp, {'compactAfterChars': 4000, 'segmentRequests': 20, 'maxModelRequests': 30})
    root = s.project('ws1')
    (root / 'big.txt').write_text('x' * 20000, encoding='utf-8')
    stub = StubModel([tool_call('read_file', {'path': 'big.txt'})] * 6 + [final('Finished after compaction.')])
    runtime.model_call = stub
    run = runtime.submit('ws1', 'a1', 'Read the big file repeatedly')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed', (stored['status'], stored.get('error'))
    assert stored['answer'] == 'Finished after compaction.'
    assert stub.saw_compaction_note, 'model never saw the compaction note'
    assert max(stub.payload_sizes[2:]) < 9000, stub.payload_sizes  # steady-state context stayed bounded (the first read of a file is legitimately full-size)
    evidence = stored.get('evidence') or {}
    assert len(evidence) == 6, sorted(evidence)  # six successful read_file executions
    messages = stored['states']['chat']['messages']
    compacted = [m for m in messages if '[compacted tool result]' in str(m.get('content', ''))]
    assert compacted, 'no compacted tool results persisted'
    assert any('(evidence: ev-' in str(m.get('content', '')) for m in compacted), 'evidence IDs lost in compaction'
    events = [e['type'] for e in s.events(run['id'])]
    assert 'context.compacted' in events, events
    print('scenario compaction: OK  max payload chars=', max(stub.payload_sizes))

async def scenario_elapsed(tmp):
    """Elapsed-time limit pauses as paused_budget; Continue opens a fresh time window and completes."""
    s, runtime = make_runtime(tmp, {'maxElapsedSeconds': 0.5, 'segmentRequests': 20, 'maxModelRequests': 30})
    stub = StubModel([tool_call('list_files', {}), final('Done after a fresh window.')], delays={0: 0.7})
    runtime.model_call = stub
    run = runtime.submit('ws1', 'a1', 'Slow start')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'paused_budget', stored['status']
    assert 'Elapsed-time limit' in stored['error'], stored['error']
    continued = runtime.continue_run(run['id'])
    assert continued['id'] == run['id']
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed', (stored['status'], stored.get('error'))
    assert stub.requests == 2
    print('scenario elapsed: OK  paused then completed after fresh window')

async def scenario_contract_nudge_unverified(tmp):
    """An unevidenced final answer gets at most two corrections, then completes as unverified."""
    s, runtime = make_runtime(tmp)
    stub = StubModel([
        tool_call('submit_contract', {'requirements': [{'description': 'Do A'}, {'description': 'Do B'}]}),
        final('Everything passed.'),
        final('Really, everything passed.'),
        final('I insist it passed.'),
    ])
    runtime.model_call = stub
    run = runtime.submit('ws1', 'a1', 'Verify things')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed_unverified', stored['status']
    assert stub.requests == 4, stub.requests  # 1 contract + final + 2 correction rounds
    corrections = [e for e in s.events(run['id']) if e['type'] == 'model.contract_correction']
    assert len(corrections) == 2, len(corrections)
    print('scenario contract nudge (unverified): OK  corrections=2 requests=', stub.requests)

async def scenario_contract_nudge_recovered(tmp):
    """After a nudge the model records real evidence and the run completes as verified."""
    s, runtime = make_runtime(tmp)
    stub = StubModel([
        tool_call('submit_contract', {'requirements': [{'description': 'Do A'}, {'description': 'Do B'}]}),
        final('Everything passed.'),
        tool_call('verify_requirement', {'requirement': 'r1', 'evidence': ['ev-1']}),
        tool_call('verify_requirement', {'requirement': 'r2', 'evidence': ['ev-1']}),
        final('Both requirements verified with recorded evidence.'),
    ])
    runtime.model_call = stub
    run = runtime.submit('ws1', 'a1', 'Verify things')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed_verified', stored['status']
    assert stub.requests == 5, stub.requests
    corrections = [e for e in s.events(run['id']) if e['type'] == 'model.contract_correction']
    assert len(corrections) == 1, len(corrections)
    print('scenario contract nudge (recovered): OK  status=', stored['status'])

async def main():
    base = tempfile.mkdtemp(prefix='context-test-')
    try:
        for i, scenario in enumerate([scenario_compaction, scenario_elapsed, scenario_contract_nudge_unverified, scenario_contract_nudge_recovered], 1):
            await scenario(base + '/s' + str(i))
        print('ALL CONTEXT SCENARIOS PASSED')
    finally:
        shutil.rmtree(base, ignore_errors=True)

asyncio.run(main())
