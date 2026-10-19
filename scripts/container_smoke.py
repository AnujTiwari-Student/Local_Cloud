"""Readiness and S3/SQS smoke checks for the Docker CI job."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from localcloud.ci import HTTPClient, smoke

client = HTTPClient('http://127.0.0.1:4566', '')
for attempt in range(60):
    try:
        client.request('GET', '/_local/health')
        break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit('Container did not become ready in 60 seconds')
print(smoke(client))
