"""
Supabase client — configured for Lambda (PgBouncer pooler endpoint).

Supabase project ref: odnptiirmmcrxelmdikb
Custom domain:        api.mirai-now.io

IMPORTANT: Use the pooler URL (port 6543) in Lambda, not the direct Postgres URL.
The pooler handles concurrent Lambda invocations without exhausting Postgres connections.
Pooler URL format: postgresql://postgres.odnptiirmmcrxelmdikb:[PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:6543/postgres?pgbouncer=true
"""

from __future__ import annotations

import os
from functools import lru_cache

from supabase import create_client, Client


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """
    Module-level Supabase client — cached across warm Lambda invocations.
    Uses the service role key for server-side operations.
    """
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)
