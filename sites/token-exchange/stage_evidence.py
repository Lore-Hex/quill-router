"""Local-only outage rig: production feed/cache with a stoppable HTTP status source.

Run with the project Python environment; never deploy this development server.
"""
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn
from fastapi import FastAPI

from trusted_router import catalog_data, token_exchange
from trusted_router.config import Settings
from trusted_router.routes.public import register_public_routes


class StatusSource(BaseHTTPRequestHandler):
    def do_GET(self):
        checked = datetime.now(UTC).isoformat()
        components = [dict(
            id=key, name=name, last_checked_at=checked, status='up',
            uptime_24h_percent=uptime, sample_count_24h=100,
            history=[dict(bucket_start=checked, status='up', sample_count=100)],
        ) for key, name, uptime in (
            ('canonical_api', 'Canonical API', 99.9485),
            ('us_east4_regional_api', 'US East Regional API', 99.691),
            ('attestation', 'Attestation', 100),
        )]
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'data': {'components': components}}).encode())


# Same test-only freshness pin as tests/catalog_freshness_freeze.py.
# Pricing still comes from real catalog route fields, never a staging constant.
catalog_data._utc_now = lambda: datetime(2000, 1, 1, tzinfo=UTC)

source = ThreadingHTTPServer(('127.0.0.1', 8095), StatusSource)
threading.Thread(target=source.serve_forever, daemon=True).start()
real_get = token_exchange._get


def local_source(client, url):
    if url.endswith('/status.json'):
        return real_get(client, 'http://127.0.0.1:8095/status.json')
    return dict(platform='gcp-confidential-space', image_digest='sha256:' + 'a' * 64,
                source_commit='staging', release_metadata_status='live')


token_exchange._get = local_source
app = FastAPI()
register_public_routes(app, Settings(environment='local', storage_backend='memory'))


@app.post('/__stage/stop-status')
def stop_status():
    source.shutdown()
    source.server_close()
    return {'status_source': 'stopped'}


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8094)
