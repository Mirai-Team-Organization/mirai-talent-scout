#!/bin/bash
# Lambda Web Adapter entrypoint — starts the FastAPI streaming agent.
# Handler: run.sh (set in template.yaml for AgentStreamFunction)
exec python -m uvicorn agent.stream_app:app --host 0.0.0.0 --port "${PORT:-8000}"
