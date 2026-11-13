"""Functional tests for Ollama auto-start: owned server launch, external-server respect, budget."""
import asyncio
import os
import shutil
import socket
import stat
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from runtime import Runtime
from store import Store

class FakeBrowser:
    def set_event_sink(self, sink):
        pass

def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

class FakeOllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/version':
            body = b'{"version": "fake-external"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass

def make_runtime(tmp, settings):
    s = Store(tmp)
    s.put('setting', 'runtime', {'endpoint': 'http://127.0.0.1:' + str(PORT), 'loadTimeout': 30, 'stallTimeout': 30, 'maxRestarts': 3, 'restartWindow': 600, 'initialBackoff': 0.1, 'maxBackoff': 0.5, **settings})
    return s, Runtime(s, FakeBrowser())

def install_fake_ollama(path):
    path.mkdir(parents=True, exist_ok=True)
    script = path / 'ollama'
    script.write_text('#!/bin/sh\nsleep 600\n', encoding='utf-8')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, 'PATH': str(path) + os.pathsep + os.environ.get('PATH', '')}

PORT = free_port()

async def scenario_starts_owned(tmp):
    """No server running -> autostart launches an owned process."""
    s, runtime = make_runtime(tmp, {'mode': 'managed', 'autoStart': True, 'probeTimeout': 2})
    os.environ.update(install_fake_ollama(Path(tmp) / 'bin'))
    try:
        result = await runtime.autostart_owned()
        assert result.get('pid') and result.get('state') == 'starting', result
        assert runtime.process and runtime.process.poll() is None
        health = await runtime.health_check()
        assert health['owned_pid'] == result['pid'], health
        again = await runtime.autostart_owned()
        assert again == {'pid': result['pid'], 'state': 'already_owned'}, again
        print('scenario starts-owned: OK  pid=', result['pid'])
    finally:
        if runtime.process and runtime.process.poll() is None:
            runtime.process.terminate()
            runtime.process.wait(5)

async def scenario_respects_external(tmp):
    """A reachable external server is never taken over."""
    s, runtime = make_runtime(tmp, {'mode': 'managed', 'autoStart': True, 'probeTimeout': 2})
    os.environ.update(install_fake_ollama(Path(tmp) / 'bin'))
    server = ThreadingHTTPServer(('127.0.0.1', PORT), FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = await runtime.autostart_owned()
        assert result == {'state': 'external', 'note': 'Server already reachable; ownership not assumed'}, result
        assert runtime.process is None
        print('scenario respects-external: OK')
    finally:
        server.shutdown()
        server.server_close()
        if runtime.process and runtime.process.poll() is None:
            runtime.process.terminate()
            runtime.process.wait(5)

async def scenario_budget_and_mode(tmp):
    """Repeated failed starts hit the budget; external mode refuses to auto-start."""
    s, runtime = make_runtime(tmp, {'mode': 'managed', 'autoStart': True, 'maxRestarts': 1, 'restartWindow': 600, 'probeTimeout': 2})
    os.environ.update(install_fake_ollama(Path(tmp) / 'bin'))
    try:
        first = await runtime.autostart_owned()
        assert first.get('pid')
        runtime.process.terminate()
        runtime.process.wait(5)
        try:
            await runtime.autostart_owned()
            raise AssertionError('budget should be exhausted')
        except ValueError as error:
            assert 'Auto-start budget exhausted' in str(error), error
        print('scenario budget: OK')
    finally:
        if runtime.process and runtime.process.poll() is None:
            runtime.process.terminate()
            runtime.process.wait(5)
    s2, runtime2 = make_runtime(tmp + '2', {'mode': 'external', 'autoStart': True, 'probeTimeout': 2})
    try:
        await runtime2.autostart_owned()
        raise AssertionError('external mode should refuse')
    except ValueError as error:
        assert 'Managed mode required' in str(error), error
    print('scenario mode-guard: OK')

async def scenario_health_loop(tmp):
    """The watchdog auto-starts an owned server when none is running."""
    s, runtime = make_runtime(tmp, {'mode': 'managed', 'autoStart': True, 'autoStartDelay': 0.2, 'interval': 0.2, 'failureThreshold': 1, 'probeTimeout': 1})
    os.environ.update(install_fake_ollama(Path(tmp) / 'bin'))
    loop = asyncio.create_task(runtime.health_loop())
    try:
        for _ in range(40):
            await asyncio.sleep(0.25)
            if runtime.process and runtime.process.poll() is None:
                break
        assert runtime.process and runtime.process.poll() is None, 'watchdog never started an owned server'
        health = await runtime.health_check()
        assert health['owned_pid'], health
        print('scenario health-loop: OK  pid=', health['owned_pid'])
    finally:
        loop.cancel()
        try:
            await asyncio.wait_for(loop, 3)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if runtime.process and runtime.process.poll() is None:
            runtime.process.terminate()
            runtime.process.wait(5)

async def main():
    base = tempfile.mkdtemp(prefix='autostart-test-')
    try:
        for i, scenario in enumerate([scenario_starts_owned, scenario_respects_external, scenario_budget_and_mode, scenario_health_loop], 1):
            await scenario(base + '/s' + str(i))
        print('ALL AUTOSTART SCENARIOS PASSED')
    finally:
        shutil.rmtree(base, ignore_errors=True)

asyncio.run(main())
