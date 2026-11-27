# day0-day2-runbook-runner

Python FastAPI service and executor for Day-0 bootstrap, Day-1 configuration drift checks, and Day-2 incident actions backed by AWS SSM and S3.

## Prerequisites

- Python 3.10+
- Docker and Docker Compose
- `curl` and `openssl`

## Local Setup

Start LocalStack and Redis:

```bash
make up
```

Run the API server:

```bash
make run
```

Run tests and benchmark:

```bash
make test
make bench
```

Stop containers:

```bash
make down
```

## Sample Request

```bash
BODY='{"runbook_id":"day1-config-drift"}'
TS=$(date -u +%s)
SIG="sha256=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac 'local-dev-runbook-secret' | awk '{print $2}')"

curl -s -X POST http://localhost:8080/v1/runbooks/run   -H "Content-Type: application/json"   -H "X-Timestamp: $TS"   -H "X-Signature-256: $SIG"   -d "$BODY"
```

## Known Limitations

- Single AWS region per process (`AWS_REGION`).
- Runbook definitions are loaded from local JSON files under `runbooks/`.
- SSM operations do not iterate over `NextToken` pagination tokens.
