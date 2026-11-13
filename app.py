# app.py
"""Local Agent dashboard and API entry point, including browser-v2 integration."""
import asyncio
import difflib
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as datetime_time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from browser import Browser
from browser_api import install_browser_api
from panel_actions import install_panel_actions
from runtime import Runtime, TOOLS, groups, validate_flow
from store import Store, digest, dump, uid

BASE = Path(__file__).resolve().parent
s = Store(os.getenv('LOCAL_AGENT_DATA', str(BASE / 'data')))
browser = Browser(s.root)
runtime = Runtime(s, browser)

if not s.all('agent'):
    for agent_id, name, vision, write in [
        ('builder', 'Builder', False, True),
        ('vision', 'Visual Analyst', True, False),
        ('reviewer', 'Reviewer', False, False),
    ]:
        model = 'gemma3:4b' if vision else 'qwen3-coder:30b'
        s.put('agent', agent_id, {
            'name': name,
            'browser': False,
            'write': write,
            'allowWorkflow': True,
            'defaultPrompt': 'Use actual tools and evidence. Never claim an action without results.',
            'mcpIds': [],
            'promptIds': [],
            'modelConfig': {
                'endpoint': 'http://127.0.0.1:11434',
                'model': model,
                'allowed': [model],
                'context': 4096,
                'output': 2048,
                'temperature': .2,
                'fallback': '',
                'vision': vision,
                'role': 'Vision' if vision else 'Coding',
            },
            'memoryRead': ['global', 'workspace'],
            'memoryWrite': 'propose',
            'capabilities': groups,
        })

if not s.all('workspace'):
    s.put('workspace', uid(), {
        'name': 'Website project',
        'description': '',
        'defaultAgent': 'builder',
        'agent': 'builder',
        'timezone': 'America/New_York',
        'paused': False,
        'instructions': '',
    })

if not s.get('setting', 'runtime', False):
    s.put('setting', 'runtime', {
        'endpoint': 'http://127.0.0.1:11434',
        'mode': 'external',
        'enabled': True,
        'interval': 10,
        'probeTimeout': 5,
        'failureThreshold': 3,
        'grace': 45,
        'loadTimeout': 300,
        'stallTimeout': 180,
        'maxRestarts': 3,
        'restartWindow': 600,
        'initialBackoff': 5,
        'maxBackoff': 60,
        'autoRestart': False,
        'autoResume': True,
        'idleSeconds': 120,
        'embeddingModel': 'embeddinggemma',
        'vectorEnabled': False,
    })

if not s.get('setting', 'resources', False):
    s.put('setting', 'resources', {
        'gpu': 'NVIDIA RTX 3060', 'vram': 12, 'ram': 64,
        'concurrency': 1, 'resident': 1, 'context': 4096,
        'output': 2048, 'handoffs': 4, 'reviews': 2,
    })

OBJECTS = {
    'conversation', 'workspace', 'agent', 'prompt', 'memory',
    'workflow', 'schedule', 'artifact', 'credential-reference',
}
ACTIVE = {'queued', 'running', 'waiting_approval', 'recovering', 'stopping'}
VERSION = '0.2.4-browser'


def next_due(schedule, after=None):
    now = after if after is not None else time.time()
    zone = ZoneInfo(schedule.get('zone') or schedule.get('timezone', 'UTC'))
    frequency = schedule.get('frequency', 'daily')
    if frequency == 'once':
        due = datetime.fromisoformat(schedule['date'] + 'T' + schedule['time']).replace(tzinfo=zone).timestamp()
        return due if due > now else None
    clock = datetime_time.fromisoformat(schedule.get('time', '09:00'))
    weekday = '1-5' if frequency == 'weekdays' else str(schedule.get('weekday', '1')) if frequency == 'weekly' else '*'
    expression = schedule['cron'] if frequency == 'cron' else f'{clock.minute} {clock.hour} * * {weekday}'
    return croniter(expression, datetime.fromtimestamp(now, zone)).get_next(datetime).timestamp()


def validate_schedule(body):
    if body.get('frequency') not in ('once', 'daily', 'weekdays', 'weekly', 'cron'):
        raise ValueError('Unknown frequency')
    next_due(body)
    s.get('agent', body['agent'])
    if body.get('target') == 'workflow':
        flow = s.get('workflow', body.get('workflow', ''))
        if body.get('workspace') and flow.get('workspace') != body['workspace']:
            raise ValueError('Workflow workspace mismatch')
    elif not body.get('task', '').strip():
        raise ValueError('Task required')
    if body.get('overlap', 'skip') not in ('skip', 'queue'):
        raise ValueError('Invalid overlap')


def validate_record(kind, body, workspace):
    if not isinstance(body, dict) or len(dump(body)) > 12000000:
        raise ValueError('Invalid or oversized record')
    if workspace:
        s.project(workspace)
    if kind == 'workspace':
        if not body.get('name', '').strip():
            raise ValueError('Name required')
        ZoneInfo(body.get('timezone', 'UTC'))
    if kind == 'conversation':
        if not workspace or not body.get('title', '').strip():
            raise ValueError('Workspace and title required')
    if kind == 'agent':
        config = body.get('modelConfig', {})
        if not body.get('name') or not config.get('model') or config['model'] not in config.get('allowed', []):
            raise ValueError('Name and allowed default model required')
        if config.get('endpoint', runtime.endpoint()).rstrip('/') != runtime.endpoint():
            raise ValueError('Use the server-wide endpoint')
        if not 512 <= int(config.get('context', 0)) <= 131072 or not 64 <= int(config.get('output', 0)) <= 32768:
            raise ValueError('Invalid token budget')
    if kind == 'memory':
        if not body.get('title') or not isinstance(body.get('value'), str):
            raise ValueError('Title/value required')
        if body.get('scope') == 'global':
            workspace = None
    if kind == 'workflow':
        validate_flow(body, s)
    if kind == 'schedule':
        validate_schedule({**body, 'workspace': workspace})
    if kind == 'credential-reference' and any(key in body for key in ('secret', 'token', 'password', 'api_key')):
        raise ValueError('Use the OS vault for secrets')
    return workspace


async def scheduler():
    while True:
        try:
            now = time.time()
            for schedule in s.all('schedule'):
                if not schedule.get('enabled') or s.get('workspace', schedule['workspace']).get('paused'):
                    continue
                due = schedule.get('nextDue')
                if due is None:
                    schedule['nextDue'] = next_due(schedule)
                    s.put('schedule', schedule['id'], schedule, schedule['workspace'])
                    continue
                if due > now:
                    continue
                active = [run for run in s.all('run', schedule['workspace'])
                          if run['status'] in ACTIVE and run.get('metadata', {}).get('schedule') == schedule['id']]
                if active and schedule.get('overlap', 'skip') == 'queue':
                    schedule['queuedDue'] = True
                    s.put('schedule', schedule['id'], schedule, schedule['workspace'])
                    continue
                occurrence = digest({'schedule': schedule['id'], 'due': due})
                schedule['nextDue'] = None if schedule.get('frequency') == 'once' else next_due(schedule, now)
                if schedule.get('frequency') == 'once':
                    schedule['enabled'] = False
                s.put('schedule', schedule['id'], schedule, schedule['workspace'])
                if s.get('occurrence', occurrence, False):
                    continue
                s.put('occurrence', occurrence, {'schedule': schedule['id'], 'due': due, 'status': 'claimed'}, schedule['workspace'])
                if active or now - due > 90 and schedule.get('missed', 'skip') == 'skip' and not schedule.get('queuedDue'):
                    s.put('occurrence', occurrence, {'status': 'skipped'}, schedule['workspace'])
                    continue
                schedule['queuedDue'] = False
                flow = s.get('workflow', schedule['workflow']) if schedule.get('target') == 'workflow' else None
                prompts = '\n\n'.join(s.get('prompt', ident)['content'] for ident in schedule.get('prompts', []) if s.get('prompt', ident, False))
                run = runtime.submit(schedule['workspace'], schedule['agent'], schedule.get('task', 'Scheduled workflow') + '\n' + prompts,
                                     workflow=flow, metadata={'schedule': schedule['id'], 'occurrence': occurrence})
                schedule['lastRun'] = run['id']
                s.put('schedule', schedule['id'], schedule, schedule['workspace'])
                s.put('occurrence', occurrence, {'status': 'dispatched', 'run': run['id']}, schedule['workspace'])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            s.log('scheduler.error', 'scheduler', {'error': str(error)})
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(application):
    runtime.resume_saved()
    tasks = [asyncio.create_task(runtime.health_loop()), asyncio.create_task(scheduler())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await runtime.stop()
        finally:
            await asyncio.to_thread(browser.stop)


app = FastAPI(title='Local Agent', version=VERSION, lifespan=lifespan)


@app.middleware('http')
async def guard(request: Request, call_next):
    if request.headers.get('host', '').split(':')[0] not in ('localhost', '127.0.0.1'):
        return JSONResponse({'detail': 'Localhost only'}, 403)
    origin = request.headers.get('origin')
    if origin and origin not in ('http://127.0.0.1:8765', 'http://localhost:8765'):
        return JSONResponse({'detail': 'Origin denied'}, 403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-local-app') != '1':
        return JSONResponse({'detail': 'App header required'}, 403)
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(ValueError)
async def invalid(request, error):
    return JSONResponse({'detail': str(error)}, 400)


@app.exception_handler(FileNotFoundError)
async def missing(request, error):
    return JSONResponse({'detail': 'File not found'}, 404)


app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')


@app.get('/')
def index():
    html = (BASE / 'static/index.html').read_text(encoding='utf-8')
    script = '<script src="/static/panel_actions.js?v=024-browser"></script>'
    if '/static/panel_actions.js' not in html:
        html = html.replace('</body>', script + '</body>') if '</body>' in html else html + script
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@app.get('/api/state')
def state():
    return {'version': VERSION, 'workspaces': s.all('workspace'), 'agents': s.all('agent'),
            'prompts': s.all('prompt'), 'runtime': runtime.config(), 'resources': s.get('setting', 'resources'),
            'health': runtime.health, 'features': {'browser_v2': True, 'owned_tabs': True, 'browser_timeline': True, 'workspace_preview': True},
            'builtins': [{'name': tool['function']['name'], 'description': tool['function']['description'],
                          'schema': tool['function']['parameters']} for tool in TOOLS]}


@app.get('/api/objects/{kind}')
def records(kind: str, workspace: str | None = None, q: str = ''):
    if kind not in OBJECTS | {'run', 'message', 'approval'}:
        raise ValueError('Unknown collection')
    return [item for item in s.all(kind, workspace) if q.casefold() in dump(item).casefold()]


@app.post('/api/objects/{kind}')
async def create(kind: str, request: Request):
    if kind not in OBJECTS:
        raise ValueError('Unknown collection')
    value = await request.json()
    body = value['body']
    workspace = validate_record(kind, body, value.get('workspace'))
    if kind == 'workflow':
        body = {**body, 'status': 'draft', 'revision': 1}
    if kind == 'schedule':
        body = {**body, 'enabled': False, 'revision': 1}
    result = s.put(kind, uid(), {**body, 'workspace': workspace}, workspace)
    s.log(kind + '.created', result['id'])
    return result


@app.put('/api/objects/{kind}/{ident}')
async def update(kind: str, ident: str, request: Request):
    if kind not in OBJECTS:
        raise ValueError('Unknown collection')
    old = s.get(kind, ident)
    value = await request.json()
    body = value['body']
    workspace = validate_record(kind, body, value.get('workspace'))
    if kind == 'conversation' and any(s.get('run', run_id).get('metadata', {}).get('conversation') == ident for run_id in list(runtime.tasks)):
        raise ValueError('Cancel/finish the active run before changing this conversation')
    if kind == 'workflow':
        body = {**body, 'revision': old.get('revision', 0) + 1, 'status': 'draft'}
    if kind == 'schedule':
        body = {**body, 'enabled': False, 'revision': old.get('revision', 0) + 1}
    result = s.put(kind, ident, {**body, 'workspace': workspace}, workspace)
    s.log(kind + '.updated', ident)
    if kind == 'memory':
        with s.db() as connection:
            connection.execute('DELETE FROM vectors WHERE id=?', (ident,))
    return result


@app.delete('/api/objects/{kind}/{ident}')
def delete(kind: str, ident: str):
    if kind not in OBJECTS:
        raise ValueError('Unknown collection')
    item = s.get(kind, ident)
    if kind == 'conversation':
        if any(s.get('run', run_id).get('metadata', {}).get('conversation') == ident for run_id in list(runtime.tasks)):
            raise ValueError('Cancel/finish the active run first')
        for message in s.all('message', item['workspace']):
            if message.get('conversation') == ident:
                s.delete('message', message['id'])
    s.delete(kind, ident)
    s.log(kind + '.deleted', ident)
    return {'deleted': ident, 'note': 'Run/browser evidence and checkpoints are retained'}


@app.post('/api/runs')
async def run(request: Request):
    body = await request.json()
    workspace = body['workspace']
    agent = s.get('agent', body['agent'])
    images = [(image if isinstance(image, str) else image['data']).split(',', 1)[-1] for image in body.get('images', [])]
    result = runtime.submit(workspace, agent['id'], body.get('text', ''), images, metadata={'conversation': body.get('conversation')}, write_policy=body.get('writePolicy'))
    s.put('message', uid(), {'workspace': workspace, 'conversation': body.get('conversation'), 'role': 'user',
          'text': body.get('text', ''), 'agent': agent['id'], 'model': agent['modelConfig']['model'],
          'images': body.get('images', []), 'run': result['id'], 'created': time.time()}, workspace)
    return result


@app.get('/api/runs/{ident}')
def get_run(ident: str, after: int = 0):
    return {**s.get('run', ident), 'events': s.events(ident, after)}


@app.get('/api/terminal')
def terminal(workspace: str, run: str | None = None, after: int = 0):
    s.project(workspace)
    if run and s.get('run', run)['workspace'] != workspace:
        raise ValueError('Run workspace mismatch')
    return s.events(run, after, workspace)


@app.post('/api/runs/{ident}/continue')
def continue_run(ident: str):
    return runtime.continue_run(ident)


@app.post('/api/runs/{ident}/cancel')
def cancel(ident: str):
    item = s.get('run', ident)
    task = runtime.tasks.get(ident)
    if task and not item.get('cancelRequested'):
        item['cancelRequested'] = True
        item['status'] = 'stopping'
        runtime.persist(item)
        task.cancel()
    return {'requested': bool(task), 'note': 'Completed effects remain; cancellation is not rollback'}


@app.post('/api/runs-stop-all')
def stop_all():
    for task in list(runtime.tasks.values()):
        task.cancel()
    return {'requested': True}


@app.get('/api/health')
def health():
    return {'health': runtime.health, 'policy': runtime.config(), 'events': list(runtime.health_events),
            'loaded': runtime.loaded, 'inferenceBusy': runtime.gpu.locked()}


@app.post('/api/health/check')
async def check_health():
    return await runtime.health_check()


@app.get('/api/models')
async def models():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            installed = await client.get(runtime.endpoint() + '/api/tags')
            installed.raise_for_status()
            loaded = await client.get(runtime.endpoint() + '/api/ps')
            loaded.raise_for_status()
        return {'installed': installed.json(), 'loaded': loaded.json()}
    except Exception as error:
        raise HTTPException(502, str(error)) from error


@app.put('/api/settings/{kind}')
async def settings(kind: str, request: Request):
    body = await request.json()
    if kind not in ('runtime', 'resources'):
        raise ValueError('Invalid setting')
    if kind == 'runtime':
        parsed = urlparse(body['endpoint'])
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Invalid endpoint')
        if runtime.tasks or runtime.process and runtime.process.poll() is None:
            raise ValueError('Stop active tasks/owned process before policy changes')
        if body['mode'] not in ('external', 'managed'):
            raise ValueError('Invalid mode')
        if body['mode'] == 'external':
            body['autoRestart'] = False
        for key, low, high in [
            ('interval', 2, 300), ('probeTimeout', 1, 60), ('failureThreshold', 2, 20),
            ('grace', 5, 600), ('loadTimeout', 30, 3600), ('stallTimeout', 30, 3600),
            ('maxRestarts', 1, 10), ('restartWindow', 60, 3600),
        ]:
            if not low <= int(body[key]) <= high:
                raise ValueError('Invalid ' + key)
        body = {**runtime.config(), **body}
    if kind == 'resources' and (body.get('concurrency') != 1 or body.get('resident') != 1):
        raise ValueError('One inference/resident target is enforced')
    return s.put('setting', kind, body)


@app.post('/api/memory/index')
async def index_memory(request: Request):
    return await runtime.index_memory((await request.json())['id'])


@app.post('/api/memory/search')
async def memory_search(request: Request):
    body = await request.json()
    return await runtime.memory_search(body['workspace'], s.get('agent', body['agent']), body['query'])


@app.post('/api/approvals')
async def propose(request: Request):
    body = await request.json()
    workspace = body['workspace']
    s.project(workspace)
    action = body['action']
    arguments = body['args']
    if action not in ('workflow.run', 'schedule.enable', 'runtime.start', 'runtime.restart'):
        raise ValueError('Unknown action')
    if action in ('workflow.run', 'schedule.enable'):
        value = s.get('workflow' if action == 'workflow.run' else 'schedule', arguments['id'])
        if value['workspace'] != workspace or value.get('revision') != arguments.get('revision'):
            raise ValueError('Target changed')
        validate_flow(value, s) if action == 'workflow.run' else validate_schedule(value)
    elif runtime.config()['mode'] != 'managed' or arguments != {'endpoint': runtime.endpoint(), 'pid': runtime.process.pid if runtime.process else None}:
        raise ValueError('Exact managed process required')
    return s.put('approval', uid(), {'workspace': workspace, 'action': action, 'args': arguments,
                 'fingerprint': digest(arguments), 'status': 'pending', 'created': time.time()}, workspace)


@app.post('/api/approvals/{ident}/decision')
async def decision(ident: str, request: Request):
    approval = s.get('approval', ident)
    body = await request.json()
    if approval['status'] != 'pending':
        raise ValueError('Already decided')
    if body.get('approve') is not True:
        approval['status'] = 'denied'
        return s.put('approval', ident, approval, approval['workspace'])
    if approval['action'] in ('tool.execute', 'node.execute'):
        parent = s.get('run', approval.get('run', ''), False)
        if not parent or parent['id'] not in runtime.tasks or parent.get('status') not in ACTIVE:
            approval['status'] = 'cancelled'
            s.put('approval', ident, approval, approval['workspace'])
            raise ValueError('The waiting run is no longer active')
        approval['status'] = 'approved'
        return s.put('approval', ident, approval, approval['workspace'])
    arguments = approval['args']
    approval['status'] = 'consumed'
    s.put('approval', ident, approval, approval['workspace'])
    try:
        if approval['action'] == 'schedule.enable':
            value = s.get('schedule', arguments['id']) if 'id' in arguments else arguments['schedule']
            current = s.get('schedule', value['id'])
            expected = digest(current) if 'schedule' in arguments else digest({'id': current['id'], 'revision': current['revision']})
            if expected != approval['fingerprint']:
                raise ValueError('Schedule changed')
            validate_schedule(current)
            current['enabled'] = True
            current['nextDue'] = next_due(current)
            result = s.put('schedule', current['id'], current, current['workspace'])
        elif approval['action'] == 'workflow.run':
            value = s.get('workflow', arguments['id'])
            if value['revision'] != arguments['revision']:
                raise ValueError('Workflow changed')
            result = runtime.submit(approval['workspace'], s.get('workspace', approval['workspace'])['defaultAgent'],
                                    'Run workflow ' + value['name'], workflow=value)
        else:
            expected = {'endpoint': runtime.endpoint(), 'pid': runtime.process.pid if runtime.process else None}
            if arguments != expected:
                raise ValueError('Process changed')
            result = await (runtime.start_owned() if approval['action'] == 'runtime.start' else runtime.restart_owned())
        approval.update(status='executed', result=result)
        s.put('approval', ident, approval, approval['workspace'])
        return approval
    except Exception as error:
        approval.update(status='failed', error=str(error))
        s.put('approval', ident, approval, approval['workspace'])
        raise


@app.post('/api/workflows/{ident}/validate')
def validate_workflow(ident: str):
    return {'valid': True, 'order': validate_flow(s.get('workflow', ident), s), 'runtimeVerified': False}


@app.get('/api/files/{workspace}')
def files(workspace: str):
    root = s.project(workspace)
    return [{'path': str(path.relative_to(root)).replace(chr(92), '/'), 'directory': path.is_dir()}
            for path in root.rglob('*') if not path.is_symlink() and path.resolve().is_relative_to(root)][:5000]


@app.get('/api/file/{workspace}')
def read_file(workspace: str, path: str):
    target = s.path(workspace, path)
    if target.stat().st_size > 2000000:
        raise ValueError('File too large')
    data = target.read_bytes()
    return {'path': path, 'content': data.decode('utf-8'), 'sha256': hashlib.sha256(data).hexdigest()}


@app.post('/api/file/{workspace}')
async def write_file(workspace: str, request: Request):
    body = await request.json()
    return s.write(workspace, body['path'], body['content'].encode(), expected=body.get('expected'))


@app.post('/api/file-action/{workspace}')
async def file_action(workspace: str, request: Request):
    body = await request.json()
    path = s.path(workspace, body.get('path', ''), root_ok=True)
    action = body['action']
    if action == 'explorer':
        if os.name != 'nt':
            raise ValueError('Windows only')
        os.startfile(str(path if path.is_dir() else path.parent))
        return {'opened': str(path)}
    if action == 'mkdir':
        path.mkdir(parents=True, exist_ok=False)
        return {'created': body['path']}
    if action == 'delete':
        if not path.is_file():
            raise ValueError('Recursive directory deletion is disabled')
        return s.write(workspace, body['path'], None, 'delete')
    if action == 'rename':
        destination = s.path(workspace, body['newPath'])
        if destination.exists():
            raise ValueError('Destination exists')
        if path.is_dir():
            path.rename(destination)
            return {'renamed': body['newPath'], 'note': 'No whole-directory rollback'}
        result = s.write(workspace, body['newPath'], path.read_bytes(), 'rename_destination')
        s.write(workspace, body['path'], None, 'rename_source')
        return result
    raise ValueError('Invalid file action')


@app.get('/api/checkpoints/{workspace}')
def checkpoints(workspace: str):
    with s.db() as connection:
        rows = connection.execute('SELECT id,path,created,reason,before IS NOT NULL AS before_exists,after IS NOT NULL AS after_exists,length(before) AS before_bytes,length(after) AS after_bytes FROM checkpoints WHERE workspace=? ORDER BY created DESC', (workspace,)).fetchall()
    return [dict(row) for row in rows]


@app.get('/api/checkpoints/{workspace}/{ident}')
def checkpoint(workspace: str, ident: str):
    value = s.checkpoint(workspace, ident)
    path = s.path(workspace, value['path'])
    current = path.read_bytes() if path.exists() else None
    def lines(data):
        return data.decode('utf-8', errors='replace').splitlines(keepends=True) if data is not None else []
    return {'path': value['path'], 'diff': ''.join(difflib.unified_diff(lines(value['before']), lines(value['after']), fromfile='before', tofile='after'))[:200000],
            'current': hashlib.sha256(current).hexdigest() if current is not None else 'missing'}


@app.post('/api/checkpoints/{workspace}/{ident}/restore')
async def restore(workspace: str, ident: str, request: Request):
    if runtime.tasks:
        raise ValueError('Stop tasks before restore')
    body = await request.json()
    if body['side'] not in ('before', 'after'):
        raise ValueError('Invalid version')
    value = s.checkpoint(workspace, ident)
    return s.write(workspace, value['path'], value[body['side']], 'restore', body['expected'])


# Legacy Browser panel routes remain for the existing base app.js.
# The new panel uses /api/browser-v2, with explicit workspace and tab targets.
@app.post('/api/browser/start')
async def browser_start(request: Request):
    body = await request.json()
    return await asyncio.to_thread(browser.start, body.get('engine', 'edge'), body.get('visible', False))


@app.post('/api/browser/stop')
async def browser_stop():
    if runtime.tasks:
        raise ValueError('Finish/cancel active runs before stopping the browser')
    await asyncio.to_thread(browser.stop)
    return {'stopped': True}


@app.get('/api/browser/tabs')
async def browser_tabs():
    try:
        return {'tabs': await asyncio.to_thread(browser.tabs), 'selected': browser.selected}
    except ValueError:
        return {'tabs': [], 'selected': None, 'connected': False}


@app.post('/api/browser/tab')
async def browser_tab_action(request: Request):
    body = await request.json()
    if body['action'] == 'select':
        return await asyncio.to_thread(browser.select, body['id'])
    if body['action'] == 'close':
        if runtime.tasks:
            raise ValueError('Use the scoped Browser panel or finish active runs before closing tabs')
        return await asyncio.to_thread(browser.close_tab, body['id'])
    if body['action'] == 'new':
        return await asyncio.to_thread(browser.new_tab)
    raise ValueError('Invalid tab action')


@app.get('/api/browser/preview')
async def browser_preview():
    return await asyncio.to_thread(browser.screenshot)


@app.get('/api/browser/read')
async def browser_read():
    return await asyncio.to_thread(browser.read)


@app.post('/api/vault')
async def vault(request: Request):
    import keyring
    body = await request.json()
    module = type(keyring.get_keyring()).__module__
    if not any(name in module for name in ('Windows', 'macOS', 'SecretService', 'kwallet')):
        raise ValueError('Supported OS-vault backend unavailable; plaintext refused')
    ident = body.get('id') or uid()
    if body.get('delete'):
        keyring.delete_password('LocalAgent', ident)
        s.delete('credential-reference', ident)
        return {'deleted': ident}
    if not body.get('secret'):
        raise ValueError('Secret required')
    keyring.set_password('LocalAgent', ident, body['secret'])
    return s.put('credential-reference', ident, {'name': body['name'], 'provider': body.get('provider', ''), 'reference': ident, 'stored': True})


@app.get('/api/audit')
def audit():
    with s.db() as connection:
        rows = connection.execute('SELECT * FROM audit ORDER BY seq DESC LIMIT 500').fetchall()
    return [{**dict(row), 'body': json.loads(row['body'])} for row in rows]


@app.post('/api/migrate')
async def migrate(request: Request):
    body = await request.json()
    imported = body['state']
    if not isinstance(imported, dict) or not isinstance(imported.get('workspaces'), list) or len(dump(imported)) > 12000000:
        raise ValueError('Invalid/oversized state')
    if not body.get('confirm'):
        return {'workspaces': [workspace.get('name') for workspace in imported['workspaces']],
                'agents': len(imported.get('agents', [])), 'prompts': len(imported.get('prompts', [])), 'note': 'Nothing written'}
    if runtime.tasks:
        raise ValueError('Stop tasks before migration')
    backup = s.root / ('pre-migration-' + str(int(time.time())) + '.sqlite')
    with s.db() as current:
        with sqlite3.connect(backup) as destination:
            current.backup(destination)
    agent_map, prompt_map, workspace_map = {}, {}, {}
    report = {'workspaces': [], 'warnings': ['Imported writes/browser disabled; schedules not activated'], 'backup': backup.name}
    for prompt in imported.get('prompts', []):
        if isinstance(prompt.get('content'), str):
            ident = uid()
            prompt_map[prompt['id']] = ident
            s.put('prompt', ident, {**prompt, 'name': prompt.get('name', 'Prompt') + ' (imported)'})
    for agent in imported.get('agents', []):
        if agent.get('modelConfig'):
            ident = uid()
            agent_map[agent['id']] = ident
            s.put('agent', ident, {**agent, 'name': agent.get('name', 'Agent') + ' (imported)', 'browser': False, 'write': False,
                  'mcpIds': [], 'promptIds': [prompt_map[prompt] for prompt in agent.get('promptIds', []) if prompt in prompt_map],
                  'memoryRead': agent.get('memoryRead', ['workspace']), 'memoryWrite': 'propose',
                  'capabilities': agent.get('capabilities', groups), 'allowWorkflow': agent.get('allowWorkflow', True)})
    for old in imported['workspaces']:
        ident = uid()
        workspace_map[old['id']] = ident
        value = {key: item for key, item in old.items() if key not in ('files', 'conversations', 'workflows', 'schedules', 'artifacts', 'checkpoints', 'approvals', 'runs', 'id')}
        value.update(name=old.get('name', 'Workspace') + ' (imported)', defaultAgent=agent_map.get(old.get('defaultAgent'), 'builder'),
                     agent=agent_map.get(old.get('agent'), 'builder'), paused=True)
        s.put('workspace', ident, value)
        report['workspaces'].append({'old': old['id'], 'new': ident, 'name': value['name']})
        for path, text in old.get('files', {}).items():
            try:
                s.write(ident, path, text.encode(), 'migration')
            except Exception as error:
                report['warnings'].append('Skipped ' + path + ': ' + str(error))
        for conversation in old.get('conversations', []):
            conversation_id = uid()
            s.put('conversation', conversation_id, {'title': conversation.get('title', 'Imported conversation'), 'archived': conversation.get('archived', False), 'workspace': ident}, ident)
            for message in conversation.get('messages', []):
                s.put('message', uid(), {**message, 'workspace': ident, 'conversation': conversation_id, 'created': message.get('created', time.time())}, ident)
        for workflow in old.get('workflows', []):
            flow = {**workflow, 'workspace': ident, 'status': 'draft', 'revision': 1}
            for node in flow.get('nodes', []):
                node['agent'] = agent_map.get(node.get('agent'), 'builder')
                node['prompts'] = [prompt_map[prompt] for prompt in node.get('prompts', []) if prompt in prompt_map]
                node['override'] = ''
            try:
                validate_flow(flow, s)
                s.put('workflow', uid(), flow, ident)
            except Exception as error:
                report['warnings'].append('Skipped workflow: ' + str(error))
        for artifact in old.get('artifacts', []):
            s.put('artifact', uid(), {**artifact, 'workspace': ident}, ident)
    for memory in imported.get('memories', []):
        if isinstance(memory.get('value'), str):
            workspace = workspace_map.get(memory.get('workspace'))
            s.put('memory', uid(), {**memory, 'workspace': workspace, 'status': 'proposed', 'pinned': False}, workspace)
    s.log('migration.completed', 'import', report)
    return report


install_panel_actions(app, s, runtime)
app.state.browser_api = install_browser_api(app, s, runtime)

if __name__ == '__main__':
    import uvicorn
    print('Local Agent ' + VERSION + ' — http://127.0.0.1:8765')
    uvicorn.run(app, host='127.0.0.1', port=8765)
