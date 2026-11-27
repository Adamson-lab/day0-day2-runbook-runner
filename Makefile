SHELL := /bin/bash
COMPOSE_FILE := deploy/docker-compose.yml

.PHONY: up down run test bench lint

up:
	docker compose -f $(COMPOSE_FILE) up -d --wait

down:
	docker compose -f $(COMPOSE_FILE) down -v

lint:
	python3 -m py_compile src/runbook_runner/engine.py src/runbook_runner/server.py tests/test_runbooks.py

run:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	RUNBOOK_BUCKET=day0-day2-runbook-logs \
	uvicorn src.runbook_runner.server:app --host 0.0.0.0 --port 8080

test:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	RUNBOOK_BUCKET=day0-day2-runbook-logs \
	python3 -m unittest discover -s tests -p "test_*.py" -v

bench:
	python3 -m unittest tests/test_runbooks.py -k bench -v
