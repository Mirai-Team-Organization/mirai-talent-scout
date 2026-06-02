.PHONY: install test test-unit test-parity test-integration lint deploy secrets-init

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt pytest pytest-asyncio pytest-mock ruff mypy

# ── Tests ─────────────────────────────────────────────────────────────────────

test: test-unit

test-unit:
	pytest tests/unit/ -v

test-parity:
	# Requires gitcheck-webapp running on localhost:3000
	# npm run dev (in gitcheck-webapp/) before running this
	pytest tests/parity/ -v -m parity

test-integration:
	# Requires real AWS credentials + GITHUB_TOKENS set
	pytest tests/integration/ -v -m integration

test-all:
	pytest tests/ -v

# ── Lint ──────────────────────────────────────────────────────────────────────

lint:
	ruff check .
	mypy agent/ scoring/ db/ mcp/ --ignore-missing-imports

fmt:
	ruff format .

# ── AWS Secrets Manager — first-time setup ────────────────────────────────────
# Run once to populate secrets from your .env file

secrets-init:
	@echo "Creating secrets in AWS Secrets Manager (eu-west-1)..."
	aws secretsmanager create-secret \
		--name mirai-talent-scout/supabase \
		--region eu-west-1 \
		--secret-string '{"url":"$(SUPABASE_URL)","service_role_key":"$(SUPABASE_SERVICE_ROLE_KEY)"}' \
		2>/dev/null || \
	aws secretsmanager update-secret \
		--secret-id mirai-talent-scout/supabase \
		--region eu-west-1 \
		--secret-string '{"url":"$(SUPABASE_URL)","service_role_key":"$(SUPABASE_SERVICE_ROLE_KEY)"}'

	aws secretsmanager create-secret \
		--name mirai-talent-scout/github \
		--region eu-west-1 \
		--secret-string '{"tokens":"$(GITHUB_TOKENS)"}' \
		2>/dev/null || \
	aws secretsmanager update-secret \
		--secret-id mirai-talent-scout/github \
		--region eu-west-1 \
		--secret-string '{"tokens":"$(GITHUB_TOKENS)"}'

	aws secretsmanager create-secret \
		--name mirai-talent-scout/orangeslice \
		--region eu-west-1 \
		--secret-string '{"api_url":"$(ORANGESLICE_API_URL)","api_key":"$(ORANGESLICE_API_KEY)"}' \
		2>/dev/null || \
	aws secretsmanager update-secret \
		--secret-id mirai-talent-scout/orangeslice \
		--region eu-west-1 \
		--secret-string '{"api_url":"$(ORANGESLICE_API_URL)","api_key":"$(ORANGESLICE_API_KEY)"}'

	aws secretsmanager create-secret \
		--name mirai-talent-scout/mcp \
		--region eu-west-1 \
		--secret-string '{"auth_secret":"$(MCP_AUTH_SECRET)"}' \
		2>/dev/null || \
	aws secretsmanager update-secret \
		--secret-id mirai-talent-scout/mcp \
		--region eu-west-1 \
		--secret-string '{"auth_secret":"$(MCP_AUTH_SECRET)"}'

	@echo "Secrets created. Run 'make deploy' next."

# ── Deploy ────────────────────────────────────────────────────────────────────

build:
	sam build

deploy: build
	sam deploy \
		--region eu-west-1 \
		--stack-name mirai-talent-scout \
		--capabilities CAPABILITY_IAM \
		--resolve-s3 \
		--no-confirm-changeset

deploy-guided: build
	sam deploy --guided --region eu-west-1

# ── DB migrations ─────────────────────────────────────────────────────────────

migrate:
	@echo "Run db/migrations/001_talent_scout_schema.sql in Supabase SQL Editor:"
	@echo "  https://supabase.com/dashboard/project/odnptiirmmcrxelmdikb/sql"

# ── Local dev ─────────────────────────────────────────────────────────────────

run-local:
	# Quick sanity check — runs the agent with a test query
	python -c "
import os; os.environ.setdefault('USE_FIXTURES', 'true')
from agent.agent import create_agent
agent = create_agent()
result = agent('Find senior Python engineers in Berlin')
print(result)
"
