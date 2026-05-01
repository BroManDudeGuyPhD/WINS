VPS Deploy — Analysis & Grade
Overall: B– (works, but a few things to fix before you spend real Anthropic dollars)
What's solid ✅
Workflow is clean — deploy.yml is minimal and correct, secrets sourced from secrets.WINS_DOPPLER_TOKEN, set -euo pipefail on the script.
Doppler integration — secrets pulled fresh on each deploy via scripts/deploy.sh:13, no secrets baked into images.
Postgres healthcheck gates everything else — services wait for DB ready (docker-compose.yml:30-32).
Compose dependency chain — db → ingestion → brain → execution is sensible.
.gitignore covers .env* — the secret-laden .env.doppler is correctly untracked.
Brain has a kill switch — decision.py:70 cleanly routes through USE_MOCK_BRAIN, lazy client init means a missing key won't crash startup.
Cost guardrails in code — Opus escalation gated on confidence ≥ 0.92 AND signal_type=catalyst (decision.py:142-146). Prompt caching enabled. Haiku pre-summarization for long text. This is the most important thing for a Claude-cost question, and it's already done well.
Issues to fix before turning on the API key 🚨
Blockers
POSTGRES_PASSWORD="changeme" in Doppler dev config (.env.doppler:24). Rotate now.
Doppler config is dev, not prd (.env.doppler:14). If this VPS is production, you should be on a prd config so dev key rotations don't accidentally break prod (and vice versa).
GitHub PAT lives in Doppler as a long-lived ghp_… classic token (.env.doppler:18). If you don't actively use it, remove it. If you do, swap to a fine-grained PAT with minimal scope and an expiry.
Should fix soon
✅ DONE — Deploy rollback. scripts/rollback.sh added; deploy.sh now writes .deploy_sha before pulling.
✅ DONE — App-level healthchecks. brain/execution/alerts Dockerfiles now have HEALTHCHECK instructions backed by /tmp/heartbeat files written in each service's main loop.
No pgdata backup. Add a nightly pg_dump cron on the VPS — scripts/backup.sh is ready, just register it:
  crontab -e
  0 2 * * * /path/to/wins/scripts/backup.sh >> /var/log/wins-backup.log 2>&1
Self-hosted runner on a public repo is risky — any PR from a fork can execute arbitrary code on your VPS via the workflow. Either: (a) make repo private, (b) restrict runner to push events only (which you've done — pull_request is not in triggers, ✅ actually OK), or (c) require approval for first-time contributors in repo settings. Worth verifying.
✅ DONE — Compose file version "3.9" obsolete line removed from docker-compose.yml.
Nice to have
Log rotation — ./logs:/app/logs bind mount with no rotation will fill the disk eventually.
No spend tracking surfacing — you log input_tokens / output_tokens / cache_read per call (decision.py:132-137), but nothing aggregates it. A daily Discord summary of token spend would be a 30-line addition and worth it before flipping USE_MOCK_BRAIN=false.
Are you ready to plug in the Claude API token?
Almost. The code path is well-designed for cost control. Before you flip USE_MOCK_BRAIN=false:

Add the key to Doppler only (never to .env.doppler directly — it gets overwritten).
Set a low monthly spend limit on the Anthropic console as a backstop.
Test once with USE_MOCK_BRAIN=false and watch one cycle's logs to confirm token counts look sane (input < ~3k, cache hits after the first call).
Then leave it on.
The Postgres password and Doppler dev-vs-prd issue are the only things I'd block on. Everything else is hardening you can layer in later.
