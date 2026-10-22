"""Read-only Paseo probe, executed as the registered owner, never as root.

The npm client supplies relay encryption. No pairing URL, credential, agent data,
or raw SDK error crosses the worker boundary. Supported npm API: Paseo 0.12.x.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

UNIT = 'paseo-daemon.service'
NOTICE = '중지·재시작하면 Paseo 연결과 하위 에이전트·터미널이 중단될 수 있습니다.'
STALE_SECONDS = 180

# Embedded so all existing *.py installation/rollback paths include the helper.
PROBE_JS = r'''
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
const [cli, home] = process.argv.slice(1);
const checks = Object.fromEntries(['http', 'local', 'relay'].map(k => [k, {state:'unknown'}]));
const clients = [];
let finished = false;
// Dependencies must not write pairing material or raw error messages to stdout.
for (const method of ['log','warn','error','info','debug']) console[method] = () => {};
function finish() {
  if (finished) return;
  finished = true;
  process.stdout.write(JSON.stringify({checks}));
  // Closing sockets explicitly also releases the unique probe session.
  for (const client of clients) { try { void client.close(); } catch {} }
  setTimeout(() => process.exit(0), 30);
}
const deadline = Date.now() + 15000;
const budget = max => Math.max(1, Math.min(max, deadline - Date.now()));
const watchdog = setTimeout(finish, 15000);
try {
  const require = createRequire(pathToFileURL(cli));
  const load = async name => import(pathToFileURL(require.resolve(name)).href);
  const {DaemonClient} = await load('@getpaseo/client/internal/daemon-client');
  const {readDaemonInstance, readLocalCredentialForTarget} = await load('@getpaseo/server/daemon-control');
  const {parseConnectionOfferFromUrl} = await load('@getpaseo/protocol/connection-offer');
  const {buildRelayWebSocketUrl, shouldUseTlsForDefaultHostedRelay} = await load('@getpaseo/protocol/daemon-endpoints');
  const ws = await load('ws');
  const WebSocket = ws.WebSocket ?? ws.default;
  const instance = await readDaemonInstance(home);
  if (!instance?.listen) {
    checks.local = {state:'failed'};
  } else {
    // Never follow a remote PASEO_HOST or expose credentials to a nonlocal host.
    const endpoint = new URL(`http://${instance.listen}`);
    if (!['127.0.0.1','localhost','[::1]'].includes(endpoint.hostname)) throw new Error('unsupported listen');
    const http = (async () => {
      try {
        const response = await fetch(new URL('/api/health', endpoint),
          {redirect:'error', signal:AbortSignal.timeout(budget(3000))});
        checks.http = {state:response.ok ? 'ok' : 'failed'};
        await response.body?.cancel();
      } catch { checks.http = {state:'failed'}; }
    })();
    const makeClient = options => {
      const client = new DaemonClient({
        clientId:`wrg-probe-${randomUUID()}`, clientType:'cli', appVersion:'0.12.0',
        reconnect:{enabled:false}, connectTimeoutMs:budget(10000),
        webSocketFactory:(url, config) => new WebSocket(url, config?.protocols, {headers:config?.headers}),
        ...options,
      });
      clients.push(client);
      return client;
    };
    const local = makeClient({url:`ws://${instance.listen}/ws`, connectTimeoutMs:budget(5000),
      localCredential:() => readLocalCredentialForTarget(home, instance.listen) ?? undefined});
    let offer;
    try {
      const start = Date.now();
      await local.connect();
      await local.ping({timeoutMs:budget(5000)});
      checks.local = {state:'ok', latency_ms:Date.now()-start};
      const pairing = await local.getDaemonPairingOffer({timeout:budget(5000)});
      if (!pairing.relayEnabled) checks.relay = {state:'disabled'};
      else offer = parseConnectionOfferFromUrl(pairing.url);
    } catch { if (checks.local.state !== 'ok') checks.local = {state:'failed'}; }
    if (offer) {
      try {
        const start = Date.now();
        const relay = makeClient({url:buildRelayWebSocketUrl({endpoint:offer.relay.endpoint,
          serverId:offer.serverId, role:'client',
          useTls:offer.relay.useTls ?? shouldUseTlsForDefaultHostedRelay(offer.relay.endpoint)}),
          e2ee:{enabled:true, daemonPublicKeyB64:offer.daemonPublicKeyB64}});
        await relay.connect();
        await relay.ping({timeoutMs:budget(10000)});
        checks.relay = {state:'ok', latency_ms:Date.now()-start};
      } catch { checks.relay = {state:'failed'}; }
    }
    await http;
  }
} catch { /* unsupported/missing SDK is unknown, never healthy */ }
clearTimeout(watchdog);
finish();
'''


def normalize(value: object, checked_at: float | None = None) -> dict:
    """Accept only small public health facts, including from owner-controlled npm."""
    checks = {}
    source = value.get('checks', {}) if isinstance(value, dict) else {}
    if not isinstance(source, dict):
        source = {}
    for key in ('http', 'local', 'relay'):
        raw = source.get(key)
        raw = raw if isinstance(raw, dict) else {}
        state = raw.get('state')
        allowed = ('ok', 'failed', 'unknown', 'disabled') if key == 'relay' else ('ok', 'failed', 'unknown')
        checks[key] = {'state': state if state in allowed else 'unknown'}
        latency = raw.get('latency_ms')
        if type(latency) is int and 0 <= latency <= 15000:
            checks[key]['latency_ms'] = latency
    states = [c['state'] for c in checks.values()]
    ok = False if 'failed' in states else None if 'unknown' in states else True
    return {'kind': 'paseo', 'ok': ok, 'checks': checks,
            'checked_at': time.time() if checked_at is None else checked_at}


def probe(home: Path) -> dict:
    if os.geteuid() == 0:
        raise PermissionError('Paseo probe must run as its owner')
    # Include the actual node prefix for npm-global installs behind ~/.local/bin/node.
    path = f'{home}/.local/bin:/usr/local/bin:/usr/bin:/bin'
    node = shutil.which('node', path=path)
    if not node:
        return normalize(None)
    node = str(Path(node).resolve())
    cli = shutil.which('paseo', path=f'{home}/.local/bin:{Path(node).parent}:{path}')
    if not cli:
        return normalize(None)
    try:
        result = subprocess.run([node, '--max-old-space-size=96', '--disable-wasm-trap-handler',
                                 '--input-type=module', '-e', PROBE_JS,
                                 str(Path(cli).resolve()), str(home / '.paseo')],
                                capture_output=True, text=True, timeout=17, cwd=str(home),
                                env={'HOME': str(home), 'PATH': path, 'LANG': 'C.UTF-8'})
        if result.returncode or len(result.stdout) > 8192:
            return normalize(None)
        return normalize(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return normalize(None)


def fresh_health(value: dict) -> dict:
    """A stalled sampler must not leave yesterday's green connection badge."""
    if time.time() - value['checked_at'] <= STALE_SECONDS:
        return value
    result = normalize(None, value['checked_at'])
    result['stale'] = True
    return result


def pending_health() -> dict:
    result = normalize(None)
    result['checked_at'] = None
    return result
