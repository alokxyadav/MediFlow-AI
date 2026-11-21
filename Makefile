.PHONY: help install seed test lint dev build up down logs clean e2e

help:
	@echo "make install   - install backend deps into backend/.venv"
	@echo "make seed      - seed the reference dataset"
	@echo "make migrate   - apply pending Alembic migrations (alembic upgrade head)"
	@echo "make migration msg='...' - generate a new migration from model changes"
	@echo "make test      - run the backend test suite"
	@echo "make e2e       - run the frontend Playwright e2e suite (needs backend running)"
	@echo "make lint      - ruff + black check (backend)"
	@echo "make dev       - run backend (reload) + notes for frontend"
	@echo "make up        - docker compose up --build (full stack)"
	@echo "make down      - docker compose down"
	@echo "make logs      - docker compose logs -f"
	@echo "make clean     - remove caches, build artifacts, local db"

install:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

seed:
	cd backend && . .venv/bin/activate && python seed.py

migrate:
	cd backend && . .venv/bin/activate && alembic upgrade head

migration:
	cd backend && . .venv/bin/activate && alembic revision --autogenerate -m "$(msg)"

test:
	cd backend && . .venv/bin/activate && rm -f mediflow.db && python seed.py && pytest -v

e2e:
	cd frontend && npm install && npx playwright install chromium && npm run build && (npm run preview -- --port 4173 & sleep 3 && npm run test:e2e; kill %1)

lint:
	cd backend && . .venv/bin/activate && pip install ruff black --quiet && ruff check . && black --check .

dev:
	@echo "Backend:  cd backend && uvicorn main:app --reload"
	@echo "Frontend: cd frontend && npm run dev"

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

clean:
	find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf backend/.pytest_cache backend/mediflow.db frontend/dist frontend/node_modules frontend/playwright-report frontend/test-results
