# panel_actions.py
"""Scoped panel-history previews and confirmed deletion.

Imported by app.py. This is a backend Python module; panel_actions.js is a
separate file in static/. Project files and persisted run state are not deleted.
"""
import hashlib
import json
import threading
import time
import uuid
from fastapi import Request

DONE = {'completed', 'completed_with_warning', 'failed', 'cancelled', 'incomplete'}


def fingerprint(rows):
    encoded = json.dumps(rows, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


class PanelActions:
    def __init__(self, store, runtime):
        self.s = store
        self.runtime = runtime
        self.plans = {}
        self._lock = threading.RLock()

    def boundary(self, workspace):
        self.s.get('workspace', workspace)
        with self.s.db() as connection:
            row = connection.execute("""SELECT COALESCE(MAX(e.seq),0) AS seq
                FROM events e JOIN records r ON r.kind='run' AND r.id=e.run
                WHERE r.workspace=?""", (workspace,)).fetchone()
        return {'workspace': workspace, 'through': row['seq']}

    def _runs(self, workspace):
        return self.s.all('run', workspace)

    def _busy(self, workspace):
        return any(run['id'] in self.runtime.tasks or run.get('status') not in DONE
                   for run in self._runs(workspace))

    def _snapshot(self, workspace, kind, cutoff=None):
        self.s.get('workspace', workspace)
        with self.s.db() as connection:
            if kind == 'events':
                if cutoff is None:
                    cutoff = self.boundary(workspace)['through']
                eligible_runs = {run['id'] for run in self._runs(workspace)
                                 if run.get('status') in DONE and run['id'] not in self.runtime.tasks}
                all_rows = [dict(row) for row in connection.execute("""SELECT e.seq,e.run,e.type,e.created
                    FROM events e JOIN records r ON r.kind='run' AND r.id=e.run
                    WHERE r.workspace=? AND e.seq<=? ORDER BY e.seq""", (workspace, cutoff))]
                rows = [row for row in all_rows if row['run'] in eligible_runs]
                note = ('Deletes saved agent/model events shown in Terminal and Activity for finished runs only. '
                        'Active, interrupted, recovering and reconciliation-required runs are protected. '
                        'Run state, conversation messages, checkpoints, browser capture history and project files remain. '
                        'Old backups and exports are not purged.')
            elif kind == 'checkpoints':
                all_rows = [dict(row) for row in connection.execute(
                    'SELECT id,path,created FROM checkpoints WHERE workspace=? ORDER BY id', (workspace,))]
                rows = [] if self._busy(workspace) else all_rows
                note = ('Permanently deletes checkpoint versions and removes rollback ability. '
                        'Current project files remain unchanged. Checkpoint deletion is blocked '
                        'while this workspace has active or unresolved runs.')
            elif kind == 'artifacts':
                all_rows = [dict(row) for row in connection.execute(
                    "SELECT id,updated FROM records WHERE kind='artifact' AND workspace=? ORDER BY id", (workspace,))]
                linked = set()
                for workflow in self.s.all('workflow', workspace):
                    for node in workflow.get('nodes', []):
                        for image in node.get('images', []):
                            if isinstance(image, str):
                                linked.add(image)
                            elif isinstance(image, dict) and image.get('id') and not image.get('data'):
                                linked.add(image['id'])
                for run in self._runs(workspace):
                    workflow = run.get('snapshot', {}).get('workflow') or {}
                    for node in workflow.get('nodes', []):
                        for image in node.get('images', []):
                            if isinstance(image, str):
                                linked.add(image)
                rows = [] if self._busy(workspace) else [row for row in all_rows if row['id'] not in linked]
                note = ('Deletes unreferenced artifact records and their stored images/text, not project files. '
                        'Saved workflow/run image references are protected. Artifact deletion is blocked '
                        'while this workspace has active or unresolved runs. Snapshots, backups and exports may retain copies.')
            else:
                raise ValueError('Only events, artifacts or checkpoints can be cleared')
        return {'rows': rows, 'cutoff': cutoff, 'protected': len(all_rows) - len(rows), 'note': note}

    def preview(self, workspace, kind):
        with self._lock:
            now = time.time()
            self.plans = {token: plan for token, plan in self.plans.items() if now - plan['created'] < 300}
            if len(self.plans) >= 100:
                raise ValueError('Too many pending previews; wait for expiry or restart')
            snapshot = self._snapshot(workspace, kind)
            token = uuid.uuid4().hex
            self.plans[token] = {
                'workspace': workspace, 'kind': kind, 'created': now,
                'rows': snapshot['rows'], 'fingerprint': fingerprint(snapshot['rows']),
                'cutoff': snapshot['cutoff'],
            }
            return {
                'token': token, 'workspace': workspace, 'kind': kind,
                'count': len(snapshot['rows']), 'protected': snapshot['protected'],
                'note': snapshot['note'], 'expires_in': 300,
                'sample': [str(row.get('seq', row.get('id'))) for row in snapshot['rows'][:8]],
            }

    def clear(self, token, confirm):
        if confirm is not True:
            raise ValueError('Explicit confirmation required')
        with self._lock:
            plan = self.plans.pop(token, None)
            if not plan or time.time() - plan['created'] >= 300:
                raise ValueError('Preview expired; preview and confirm again')
            workspace = plan['workspace']
            kind = plan['kind']
            with self.s.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                current = self._snapshot(workspace, kind, plan['cutoff'])
                if fingerprint(current['rows']) != plan['fingerprint']:
                    raise ValueError('Targets changed; preview and confirm again')
                if kind == 'events':
                    connection.executemany('DELETE FROM events WHERE seq=?', [(row['seq'],) for row in plan['rows']])
                elif kind == 'checkpoints':
                    connection.executemany('DELETE FROM checkpoints WHERE id=? AND workspace=?',
                                           [(row['id'], workspace) for row in plan['rows']])
                else:
                    connection.executemany("DELETE FROM records WHERE kind='artifact' AND id=? AND workspace=?",
                                           [(row['id'], workspace) for row in plan['rows']])
            result = {'workspace': workspace, 'kind': kind, 'deleted': len(plan['rows']),
                      'protected': current['protected'], 'note': 'No tool executed, undone or replayed; project files unchanged'}
            self.s.log('panel_history.cleared', workspace, result)
            return result


def install_panel_actions(app, store, runtime):
    actions = PanelActions(store, runtime)
    app.state.panel_actions = actions

    @app.get('/api/panel-actions/boundary')
    async def boundary(workspace: str):
        return actions.boundary(workspace)

    @app.post('/api/panel-actions/preview')
    async def preview(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('JSON object required')
        return actions.preview(body['workspace'], body['kind'])

    @app.post('/api/panel-actions/clear')
    async def clear(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('JSON object required')
        return actions.clear(body.get('token'), body.get('confirm'))

    return actions
