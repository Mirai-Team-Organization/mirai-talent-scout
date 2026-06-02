# Deferred work

Items surfaced during engineering review but not in scope for the initial streaming implementation.

---

## P2 — Rate limiting on `/agent` endpoint

**Risk:** billing exposure. Each request invokes Claude Sonnet + 4 Strands tools; a
runaway client (or missing auth in staging) could rack up significant Bedrock costs.

**What to do:**
- Add per-token (or per-IP fallback) rate limiting in the FastAPI middleware.
- Recommended library: [`slowapi`](https://github.com/laurentS/slowapi) (thin wrapper
  around `limits`; works with FastAPI/Starlette out of the box).
- Suggested limit: 10 req/min per Bearer token, 2 concurrent in-flight per token.
- Store state in Redis (ElastiCache Serverless) or use in-process limits acceptable
  for single-instance provisioned concurrency (ProvisionedConcurrentExecutions: 1).

**Files:** `agent/stream_app.py`, `requirements.txt`, `template.yaml`

---

## P3 — Bump Lambda Web Adapter (LWA) layer version

**Why it matters:** LWA is pinned to `:27` in `template.yaml`. New releases fix bugs,
improve cold-start time, and add ARM64 optimisations.

**What to do:**
- Periodically check: https://github.com/awslabs/aws-lambda-web-adapter/releases
- Update the layer ARN in `template.yaml`:
  ```yaml
  Layers:
    - !Sub arn:aws:lambda:${AWS::Region}:753240598075:layer:LambdaAdapterLayerArm64:<NEW_VERSION>
  ```
- Re-run `make deploy` after bumping.
- Set a calendar reminder quarterly (or subscribe to GitHub release notifications).

**Files:** `template.yaml` line 89
