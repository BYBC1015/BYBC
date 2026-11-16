"""Independent read-only /models checks; never submits model inference."""
import ipaddress
import re
import threading
import time
from urllib.parse import urlsplit

import httpx


def use_environment_proxy(url):
    host = (urlsplit(url).hostname or '').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        return False
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True


def probe_service(service):
    url = service['base_url'].rstrip('/') + '/models'
    # Identity encoding keeps byte limits meaningful before decompression.
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    key = service.get('api_key', '')
    if key:
        headers['Authorization'] = 'Bearer ' + key
    deadline = time.monotonic() + 15
    try:
        # The wall budget is checked at each transport chunk. One currently
        # blocked read may finish up to four seconds after that budget; avoid
        # buffered iter_bytes(chunk_size=...) which can hide a trickle forever.
        with httpx.Client(timeout=httpx.Timeout(4, connect=5), follow_redirects=False, trust_env=use_environment_proxy(url)) as client:
            with client.stream('GET', url, headers=headers) as response:
                if time.monotonic() >= deadline:
                    return {'code': 'timeout'}
                if response.status_code in {401, 403}:
                    return {'code': 'auth'}
                if response.status_code != 200:
                    return {'code': 'http'}
                if response.headers.get('content-encoding', 'identity').lower() not in {'', 'identity'}:
                    return {'code': 'invalid'}
                content = bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic() >= deadline:
                        return {'code': 'timeout'}
                    if len(content) + len(chunk) > 1024 * 1024:
                        return {'code': 'invalid'}
                    content.extend(chunk)
                import json
                value = json.loads(content)
                rows = value.get('data') if isinstance(value, dict) else None
                if not isinstance(rows, list):
                    return {'code': 'invalid'}
                # Bound IDs before the completion request as well as on the
                # server, and never forward a reflected credential as a model.
                models = list(dict.fromkeys(r['id'] for r in rows[:1000]
                    if isinstance(r, dict) and isinstance(r.get('id'), str)
                    and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}', r['id'])
                    and not (key and key in r['id'])))[:500]
                return {'code': 'ready', 'models': models}
    except httpx.TimeoutException:
        return {'code': 'timeout'}
    except (httpx.HTTPError, OSError):
        return {'code': 'network'}
    except (ValueError, TypeError, KeyError):
        return {'code': 'invalid'}


class ModelServiceProbeWorker:
    def __init__(self, client, interval_seconds=15):
        self.client = client
        self.interval = interval_seconds
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='hub-model-probes', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def tick(self):
        payload = self.client._json('POST', '/api/model-services/probes/claim', {})
        job = payload.get('job') if isinstance(payload, dict) else None
        if not job:
            return
        result = probe_service(job['service'])
        self.client._json('POST', f'/api/model-services/probes/{int(job["id"])}/complete',
                          {**result, 'claim_token': job['claim_token']})

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.tick()
            except Exception:
                # Network/probe failure never takes down execution lanes or logs secrets.
                pass
            self.stopped.wait(self.interval)
