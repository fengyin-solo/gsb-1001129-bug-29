.PHONY: install backend frontend test

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test:
	cd backend && python3 -m tests.test_lighting_board && python3 -m tests.test_lighting_http
	cd frontend && npm run typecheck
