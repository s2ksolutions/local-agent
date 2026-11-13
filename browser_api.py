# browser_api.py
"""Dashboard APIs for owned tabs, local previews and durable browser timelines.

app.py must call install_browser_api(app, store, runtime). Existing dashboard
Host/Origin/write-header protections must remain enabled. No raw CDP or shell
endpoint is exposed. These routes are for the trusted local human interface;
agent actions go through BrowserTools and Runtime permission/approval checks.
"""
import asyncio
import time
from fastapi import Request


DONE = {'completed', 'completed_with_warning', 'failed', 'cancelled', 'incomplete'}


def install_browser_api(app, store, runtime):
    browser = runtime.browser
    timeline = runtime.browser_events
    previews = runtime.preview
    service = runtime.browser_tools
    deletion_scopes = {}

    def workspace_record(workspace):
        if not isinstance(workspace, str) or not workspace:
            raise ValueError('Workspace ID required')
        return store.get('workspace', workspace)

    def owner_for(workspace, conversation=None):
        workspace_record(workspace)
        if conversation:
            item = store.get('conversation', conversation)
            if item.get('workspace') != workspace:
                raise ValueError('Conversation workspace mismatch')
        # Conversation ownership permits collaboration with agent-created tabs.
        # Without a conversation, UI tabs remain in a separate workspace-UI scope.
        return {
            'workspace': workspace,
            'conversation': conversation or None,
            'run': 'ui:' + workspace + ':' + (conversation or 'workspace'),
            'agent': 'user',
        }

    def is_running():
        return browser.process is not None and browser.process.poll() is None

    def unresolved_runs(workspace=None):
        return [run for run in store.all('run', workspace)
                if run['id'] in runtime.tasks or run.get('status') not in DONE]

    def protect_workspace(workspace):
        if unresolved_runs(workspace):
            raise ValueError('Finish, cancel or reconcile this workspace\'s active/unresolved runs first')

    def protect_tab(tab):
        owner = tab.get('owner') or {}
        for run in unresolved_runs(owner.get('workspace')):
            conversation = run.get('metadata', {}).get('conversation')
            if owner.get('conversation') and conversation == owner['conversation']:
                raise ValueError('This conversation has an active/unresolved run; do not disrupt its browser tab')
            if not owner.get('conversation') and owner.get('run') == run['id']:
                raise ValueError('This tab belongs to an active/unresolved run')

    def live_tabs(workspace):
        workspace_record(workspace)
        if not is_running():
            return []
        known = {(tab['session_id'], tab['tab_id']): tab
                 for tab in timeline.tabs(workspace, include_closed=True)}
        tabs = []
        for raw in browser.tabs(include_closed=True):
            owner = raw.get('owner') or {}
            if owner.get('workspace') != workspace:
                continue
            tab = dict(raw)
            previous = known.get((tab['session_id'], tab['id']))
            if tab.get('closed') is True and previous and previous.get('closed'):
                tab['closed'] = previous['closed']
            timeline.bind_tab(tab)
            if tab.get('session_id') != browser.session_id or tab.get('closed'):
                continue
            tab['alias'] = 'tab-' + tab['id'][:8]
            tab['owner_conversation'] = owner.get('conversation')
            tab['owner_run'] = owner.get('run')
            tab['owner_agent'] = owner.get('agent')
            tab['error_count_kind'] = 'captured_error_events_not_unique_root_causes'
            tabs.append(tab)
        return tabs

    def current_tab(workspace, tab_id):
        matches = [tab for tab in live_tabs(workspace) if tab['id'] == tab_id]
        if len(matches) != 1:
            raise ValueError('Tab is closed, unavailable or belongs to another workspace')
        return matches[0]

    def known_tab(workspace, tab_id, session_id=None):
        candidates = timeline.tabs(workspace, include_closed=True)
        candidates = [tab for tab in candidates if tab['tab_id'] == tab_id
                      and (session_id is None or tab['session_id'] == session_id)]
        if len(candidates) != 1:
            raise ValueError('Historical tab is unknown or ambiguous; supply session_id')
        return candidates[0]

    def sync_tab_events(tab):
        if not is_running() or tab.get('session_id') != browser.session_id or tab.get('closed'):
            return
        tab_id = tab.get('id') or tab.get('tab_id')
        owner = tab.get('owner')
        if not owner:
            owner = {'workspace': tab['workspace'], 'conversation': tab.get('conversation'),
                     'run': tab.get('owner_run'), 'agent': tab.get('owner_agent')}
        for event in browser.read_events(tab_id=tab_id, owner=owner, after=0, limit=5000):
            timeline.record(event)

    def focused_tab(workspace, conversation):
        owner = owner_for(workspace, conversation)
        focus = store.get('browser_focus', service._focus_key(owner), False)
        return focus.get('tab_id') if focus else None

    def state_data(workspace, conversation=None, include_closed=False):
        owner_for(workspace, conversation)
        tabs = live_tabs(workspace)
        for tab in tabs:
            tab['current_conversation'] = (tab.get('owner') or {}).get('conversation') == (conversation or None)
        focused = focused_tab(workspace, conversation)
        if not any(tab['id'] == focused for tab in tabs):
            focused = None
        history = []
        if include_closed:
            active_keys = {(tab['session_id'], tab['id']) for tab in tabs}
            history = [tab for tab in timeline.tabs(workspace, include_closed=True)
                       if (tab['session_id'], tab['tab_id']) not in active_keys]
            history = sorted(history, key=lambda tab: tab['created'], reverse=True)[:200]
        return {
            'running': is_running(),
            'workspace': workspace,
            'conversation': conversation or None,
            'session_id': browser.session_id if is_running() else None,
            'engine': browser.engine,
            'headless': not browser.visible,
            'tabs': tabs,
            'history_tabs': history,
            'agent_focus_tab_id': focused,
            'legacy_selected_tab_id': browser.selected,
            'previews': previews.list(workspace),
            'can_stop': not bool(unresolved_runs()),
            'request_body_capture': bool(browser.capture_post_data),
            'header_capture': bool(browser.capture_headers),
            'note': 'The dashboard inspection tab and agent action target may differ. Actions use explicit tab IDs. Tab ownership is not cookie/session isolation.',
        }

    def validate_json_object(value):
        if not isinstance(value, dict):
            raise ValueError('JSON object required')
        return value

    @app.get('/api/browser-v2/state')
    async def get_state(workspace: str, conversation: str | None = None, include_closed: bool = False):
        return await asyncio.to_thread(state_data, workspace, conversation, include_closed)

    @app.post('/api/browser-v2/start')
    async def start(request: Request):
        body = validate_json_object(await request.json())
        workspace = body['workspace']
        conversation = body.get('conversation')
        owner = owner_for(workspace, conversation)
        await asyncio.to_thread(browser.start, engine=body.get('engine', 'edge'), visible=False, owner=owner)
        tabs = await asyncio.to_thread(live_tabs, workspace)
        scoped = [tab for tab in tabs if (tab.get('owner') or {}).get('conversation') == owner['conversation']]
        if not scoped:
            tab = await asyncio.to_thread(browser.open_tab, owner=owner, label='App preview', purpose='Testing')
            timeline.bind_tab(tab)
        return await asyncio.to_thread(state_data, workspace, conversation)

    @app.post('/api/browser-v2/stop')
    async def stop(request: Request):
        body = validate_json_object(await request.json())
        workspace_record(body['workspace'])
        if body.get('confirm') is not True:
            raise ValueError('Confirm stopping the shared managed browser and all its tabs')
        if unresolved_runs():
            raise ValueError('The browser is shared; finish, cancel or reconcile active/unresolved runs before stopping it')
        await asyncio.to_thread(browser.stop)
        return {'stopped': True, 'note': 'Application-owned browser only; other browsers and preview servers are unchanged'}

    @app.post('/api/browser-v2/open')
    async def open_tab(request: Request):
        body = validate_json_object(await request.json())
        workspace = body['workspace']
        owner = owner_for(workspace, body.get('conversation'))
        tab = await asyncio.to_thread(browser.open_tab, url=body.get('url', 'about:blank'),
                                      label=body.get('label', 'Browser tab'), purpose=body.get('purpose', 'Manual'), owner=owner)
        timeline.bind_tab(tab)
        return {'tab': tab, 'state': await asyncio.to_thread(state_data, workspace, body.get('conversation'))}

    @app.post('/api/browser-v2/open-local')
    async def open_local(request: Request):
        body = validate_json_object(await request.json())
        workspace = body['workspace']
        owner = owner_for(workspace, body.get('conversation'))
        preview = await asyncio.to_thread(previews.start, workspace, path=body['path'], owner=owner)
        tabs = await asyncio.to_thread(live_tabs, workspace)
        existing = next((tab for tab in tabs if tab.get('url') == preview['url']
                         and (tab.get('owner') or {}).get('conversation') == owner['conversation']), None)
        if existing:
            await asyncio.to_thread(browser.select, existing['id'], owner=existing['owner'])
            tab = existing
        else:
            tab = await asyncio.to_thread(browser.open_tab, url=preview['url'], label=body.get('label', 'App preview'),
                                          purpose=body.get('purpose', 'Testing'), owner=owner)
            timeline.bind_tab(tab)
        return {'tab': tab, 'preview': {key: preview[key] for key in ('workspace', 'preview_id', 'origin', 'url', 'running', 'read_only')},
                'reused': bool(existing), 'note': 'Existing pages are not silently reloaded; use Reload for fresh verification.'}

    @app.post('/api/browser-v2/select')
    async def select_tab(request: Request):
        body = validate_json_object(await request.json())
        tab = await asyncio.to_thread(current_tab, body['workspace'], body['tab_id'])
        # Selecting a human inspection tab does not overwrite browser_focus.
        return await asyncio.to_thread(browser.select, tab['id'], owner=tab['owner'])

    @app.post('/api/browser-v2/navigate')
    async def navigate(request: Request):
        body = validate_json_object(await request.json())
        tab = await asyncio.to_thread(current_tab, body['workspace'], body['tab_id'])
        protect_tab(tab)
        return await asyncio.to_thread(browser.navigate, body['url'], tab_id=tab['id'], owner=tab['owner'])

    @app.post('/api/browser-v2/reload')
    async def reload_tab(request: Request):
        body = validate_json_object(await request.json())
        tab = await asyncio.to_thread(current_tab, body['workspace'], body['tab_id'])
        protect_tab(tab)
        return await asyncio.to_thread(browser.reload, tab_id=tab['id'], owner=tab['owner'], ignore_cache=True)

    @app.post('/api/browser-v2/close-tab')
    async def close_tab(request: Request):
        body = validate_json_object(await request.json())
        tab = await asyncio.to_thread(current_tab, body['workspace'], body['tab_id'])
        protect_tab(tab)
        if tab.get('pinned'):
            raise ValueError('This tab is pinned; unpin it before closing')
        await asyncio.to_thread(browser.close_tab, tab['id'], owner=tab['owner'])
        for closed in browser.tabs(owner=tab['owner'], include_closed=True):
            if closed['id'] == tab['id']:
                timeline.bind_tab(closed)
        return {'closed': True, 'tab_id': tab['id']}

    @app.post('/api/browser-v2/context')
    async def update_context(request: Request):
        body = validate_json_object(await request.json())
        tab = await asyncio.to_thread(current_tab, body['workspace'], body['tab_id'])
        if 'pinned' in body and type(body['pinned']) is not bool:
            raise ValueError('pinned must be a boolean')
        updated = await asyncio.to_thread(browser.set_tab_context, tab['id'], label=body.get('label'),
                                          purpose=body.get('purpose'), pinned=body.get('pinned'), owner=tab['owner'])
        timeline.bind_tab(updated)
        return updated

    @app.get('/api/browser-v2/preview')
    async def screenshot(workspace: str, tab_id: str):
        tab = await asyncio.to_thread(current_tab, workspace, tab_id)
        image = await asyncio.to_thread(browser.screenshot, tab_id=tab_id, owner=tab['owner'])
        return {**image, 'workspace': workspace, 'session_id': tab['session_id'],
                'title': tab.get('title'), 'url': tab.get('url'), 'label': tab.get('label'),
                'purpose': tab.get('purpose'), 'note': 'Captured screenshot, not a continuous video stream'}

    @app.get('/api/browser-v2/read')
    async def read(workspace: str, tab_id: str):
        tab = await asyncio.to_thread(current_tab, workspace, tab_id)
        return await asyncio.to_thread(browser.read, tab_id=tab_id, owner=tab['owner'])

    async def event_data(workspace, tab_id=None, session_id=None, conversation=None, after=0,
                         before=None, order='asc', limit=500, category=None, event_type=None,
                         method=None, status=None, errors_only=False, search=''):
        owner_for(workspace, conversation)
        capture = {'state': 'not_checked', 'coverage': 'Persisted capture only; no universal zero-error claim'}
        if tab_id:
            live = await asyncio.to_thread(live_tabs, workspace)
            active = next((tab for tab in live if tab['id'] == tab_id
                           and (session_id is None or tab['session_id'] == session_id)), None)
            if active:
                await asyncio.to_thread(sync_tab_events, active)
                session_id = active['session_id']
                capture = {'state': active.get('capture_state', 'not_checked'),
                           'capture_started': active.get('capture_started'),
                           'coverage': 'Events observed since attachment; capture can miss earlier activity and events may still arrive'}
            else:
                historical = await asyncio.to_thread(known_tab, workspace, tab_id, session_id)
                session_id = historical['session_id']
                capture = {'state': 'historical', 'capture_started': historical.get('capture_started'),
                           'coverage': 'Saved historical capture, not a live check'}
        actual_limit = max(1, min(int(limit), 2000))
        rows = await asyncio.to_thread(timeline.read, workspace, tab_id=tab_id, session_id=session_id,
            conversation=conversation, after=after, before=before, order=order, limit=actual_limit,
            category=category, event_type=event_type, method=method, status=status,
            errors_only=errors_only, search=search)
        session_context = []
        if tab_id and session_id and not after and before is None:
            for lifecycle_type in ('browser.connected', 'browser.started'):
                session_context.extend(await asyncio.to_thread(timeline.read, workspace,
                    session_id=session_id, event_type=lifecycle_type, order='asc', limit=10))
        return {'workspace': workspace, 'tab_id': tab_id, 'session_id': session_id,
                'events': rows, 'session_context': sorted(session_context, key=lambda row: row['seq']),
                'capture': capture, 'count': len(rows), 'possibly_more': len(rows) == actual_limit,
                'next_cursor': max((row['seq'] for row in rows), default=after),
                'oldest_cursor': min((row['seq'] for row in rows), default=before), 'order': order}

    @app.get('/api/browser-v2/events')
    async def events(workspace: str, tab_id: str | None = None, session_id: str | None = None,
                     conversation: str | None = None, after: int = 0, before: int | None = None,
                     order: str = 'asc', limit: int = 500, category: str | None = None,
                     event_type: str | None = None, method: str | None = None,
                     status: int | None = None, errors_only: bool = False, search: str = ''):
        return await event_data(workspace, tab_id, session_id, conversation, after, before, order,
                                limit, category, event_type, method, status, errors_only, search)

    @app.get('/api/browser-v2/errors')
    async def errors(workspace: str, tab_id: str, session_id: str | None = None, after: int = 0, limit: int = 500):
        return await event_data(workspace, tab_id=tab_id, session_id=session_id, after=after,
                                limit=limit, errors_only=True)

    @app.post('/api/browser-v2/preview/start')
    async def preview_start(request: Request):
        body = validate_json_object(await request.json())
        owner = owner_for(body['workspace'], body.get('conversation'))
        result = await asyncio.to_thread(previews.start, body['workspace'], path=body['path'], owner=owner)
        return {key: result[key] for key in ('workspace', 'preview_id', 'origin', 'url', 'running', 'read_only')}

    @app.post('/api/browser-v2/preview/stop')
    async def preview_stop(request: Request):
        body = validate_json_object(await request.json())
        workspace_record(body['workspace'])
        protect_workspace(body['workspace'])
        if body.get('confirm') is not True:
            raise ValueError('Confirm stopping this workspace\'s preview server')
        return await asyncio.to_thread(previews.stop, body['workspace'])

    @app.get('/api/browser-v2/previews')
    async def preview_list(workspace: str):
        workspace_record(workspace)
        return await asyncio.to_thread(previews.list, workspace)

    @app.post('/api/browser-v2/history/preview')
    async def history_preview(request: Request):
        body = validate_json_object(await request.json())
        workspace = body['workspace']
        protect_workspace(workspace)
        plan = await asyncio.to_thread(timeline.preview_clear, workspace,
                                      tab_id=body.get('tab_id'), session_id=body.get('session_id'))
        now = time.time()
        expired = [token for token, value in deletion_scopes.items() if now - value['created'] >= 300]
        for token in expired:
            deletion_scopes.pop(token, None)
        deletion_scopes[plan['token']] = {'workspace': workspace, 'created': now}
        return plan

    @app.post('/api/browser-v2/history/clear')
    async def history_clear(request: Request):
        body = validate_json_object(await request.json())
        workspace = body['workspace']
        workspace_record(workspace)
        token = body.get('token')
        target = deletion_scopes.get(token)
        if not target or target['workspace'] != workspace or time.time() - target['created'] >= 300:
            raise ValueError('Deletion preview expired or workspace changed')
        protect_workspace(workspace)
        if body.get('confirm') is not True:
            raise ValueError('Explicit confirmation is required')
        deletion_scopes.pop(token, None)
        return await asyncio.to_thread(timeline.clear, token, confirm=True)

    return {'state': state_data, 'live_tabs': live_tabs, 'current_tab': current_tab,
            'event_data': event_data, 'owner_for': owner_for}
