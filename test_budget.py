"""Functional smoke tests for the progress-aware, resumable budget (no Ollama needed)."""
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
    """Scripted model: request_handler(payload) -> message dict."""
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

def make_runtime(tmp, settings):
    s = Store(tmp)
    s.put('setting', 'runtime', {'endpoint': 'http://127.0.0.1:11434', 'loadTimeout': 30, 'stallTimeout': 30, **settings})
    s.put('workspace', 'ws1', {'name': 'Test', 'defaultAgent': 'a1', 'timezone': 'America/New_York', 'paused': False, 'instructions': ''})
    s.put('agent', 'a1', {'name': 'Tester', 'modelConfig': {'model': 'stub', 'allowed': ['stub'], 'context': 4096, 'output': 512, 'temperature': 0.1, 'vision': False}, 'capabilities': ['files', 'workspaces', 'agents'], 'write': True, 'browser': False, 'memoryRead': [], 'memoryWrite': 'disabled', 'promptIds': [], 'defaultPrompt': '', 'allowWorkflow': False})
    return s, Runtime(s, FakeBrowser())

async def drive(runtime, run_id, timeout=15):
    task = runtime.tasks.get(run_id)
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout)

async def scenario1(tmp):
    """Progress renews segments; total cap pauses as paused_budget; Continue resumes the SAME run to completion."""
    s, runtime = make_runtime(tmp, {'segmentRequests': 3, 'maxModelRequests': 7, 'maxSegments': 8})
    phase = {'mode': 'loop'}

    def handler(n, payload):
        return tool_call('list_files', {}) if phase['mode'] == 'loop' else final('All done after continuation.')

    runtime.model_call = StubModel(handler)
    run = runtime.submit('ws1', 'a1', 'Keep listing files')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'paused_budget', stored['status']
    b = stored['budget']
    assert b['model_requests'] == 7 and b['tool_executions'] == 6 and b['segments'] >= 2, b
    assert b['segment_progress'] is True, b  # the pause came from the total cap, not a loop
    assert s.get('message', 'reply-' + run['id'])['text'].startswith('Run paused at its execution budget')
    assert len(stored['states']['chat'].get('pending_calls') or []) == 1  # unexecuted call stays pending, not replayed blind
    history_before = len(stored['states']['chat']['messages'])
    # Continue: same run id, preserved messages, fresh grant, completes.
    phase['mode'] = 'final'
    continued = runtime.continue_run(run['id'])
    assert continued['id'] == run['id']
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'completed', (stored['status'], stored.get('error'))
    assert stored['answer'] == 'All done after continuation.'
    b = stored['budget']
    assert b['model_requests'] == 8 and b['total_limit'] == 13, b  # 7 + grant of 6
    assert b['tool_executions'] == 7, b  # the pending call was dispatched first on resume
    assert len(stored['states']['chat']['messages']) >= history_before  # tool-call history preserved
    assert stored['recovery_attempt'] == 1
    print('scenario1 paused_budget + Continue: OK  ', {k: b[k] for k in ('model_requests', 'tool_executions', 'segments', 'total_limit')})

async def scenario2(tmp):
    """Identical failing action repeated: correction at 3, hard stop at 4."""
    s, runtime = make_runtime(tmp, {'segmentRequests': 12, 'maxModelRequests': 30})
    runtime.model_call = StubModel(lambda n, payload: tool_call('read_file', {'path': 'missing.txt'}))
    run = runtime.submit('ws1', 'a1', 'Read a file that does not exist')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'incomplete', stored['status']
    assert 'failed identically' in stored['error'], stored['error']
    assert stored['budget']['model_requests'] == 4, stored['budget']
    print('scenario2 repeated-identical-failure stop: OK  requests=', stored['budget']['model_requests'])

async def scenario3(tmp):
    """Different failing actions each round: no-progress correction at 6, stop at 10."""
    s, runtime = make_runtime(tmp, {'segmentRequests': 12, 'maxModelRequests': 30})
    runtime.model_call = StubModel(lambda n, payload: tool_call('read_file', {'path': 'missing-' + str(n) + '.txt'}))
    run = runtime.submit('ws1', 'a1', 'Keep reading different missing files')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'incomplete', stored['status']
    assert 'no successful action' in stored['error'], stored['error']
    assert stored['budget']['model_requests'] == 10, stored['budget']
    print('scenario3 no-progress window stop: OK  requests=', stored['budget']['model_requests'])

async def scenario4(tmp):
    """Budget exhausted with NO segment progress stops as paused_budget with a loop hint."""
    s, runtime = make_runtime(tmp, {'segmentRequests': 3, 'maxModelRequests': 30, 'maxSegments': 8})
    runtime.model_call = StubModel(lambda n, payload: tool_call('read_file', {'path': 'missing-' + str(n) + '.txt'}))
    run = runtime.submit('ws1', 'a1', 'Fail differently inside each segment')
    await drive(runtime, run['id'])
    stored = s.get('run', run['id'])
    assert stored['status'] == 'paused_budget', stored['status']
    assert 'without segment progress' in stored['error'], stored['error']
    assert stored['budget']['segment_requests'] == 3, stored['budget']
    print('scenario4 no-progress segment pause: OK  requests=', stored['budget']['model_requests'])

async def main():
    base = tempfile.mkdtemp(prefix='budget-test-')
    try:
        for i, scenario in enumerate([scenario1, scenario2, scenario3, scenario4], 1):
            await scenario(base + '/s' + str(i))
        print('ALL SCENARIOS PASSED')
    finally:
        shutil.rmtree(base, ignore_errors=True)

asyncio.run(main())
