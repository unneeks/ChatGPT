# Azure Deployment Guide — reqsmith

> **Architecture**: Single Azure Container App (consumption plan, min replicas=0 / scale-to-zero) + Azure Postgres Flexible Server B1ms. SLA timers are driven by a GitHub Actions cron job that pings `/internal/tick` every 10 minutes.

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| `az` CLI | ≥ 2.60 | Azure provisioning |
| Docker | any | Build + push container image |
| Python 3.11+ | local | Run eval harness |

You also need:
- An Azure subscription (free tier works for everything except Container Registry Basic ~$5/mo)
- An Entra tenant for the Bot Service and Graph daemon app
- A Jira Cloud workspace with a pilot project
- An Anthropic API key

---

## Step 1 — Provision Azure infrastructure

```bash
cd reqsmith
export REQSMITH_SUFFIX=mybank   # short unique suffix for all resource names
bash deploy/azure-setup.sh
```

The script creates:
- Resource group `rg-reqsmith-{SUFFIX}`
- Container Registry Basic (image storage)
- Postgres Flexible Server B1ms (free 12 months for new accounts; Neon free tier as fallback)
- Container Apps environment + app (consumption plan — free grant)
- Bot Service F0 registration (free)

Save the printed values: `APP_URL`, `TICK_SECRET`, `DB_PASSWORD`.

---

## Step 2 — Set secrets on the Container App

```bash
RG="rg-reqsmith-${REQSMITH_SUFFIX}"
CA="ca-reqsmith-${REQSMITH_SUFFIX}"

az containerapp update --name "$CA" --resource-group "$RG" \
  --set-env-vars \
  "JIRA_BASE_URL=https://YOUR_ORG.atlassian.net" \
  "JIRA_EMAIL=your-service-account@org.com" \
  "JIRA_API_TOKEN=your-jira-token" \
  "JIRA_WEBHOOK_SECRET=your-webhook-secret" \
  "JIRA_PROJECT_KEY=BANK" \
  "ANTHROPIC_API_KEY=sk-ant-..." \
  "BOT_APP_ID=your-bot-app-id" \
  "BOT_APP_PASSWORD=your-bot-password" \
  "TEAMS_TENANT_ID=your-tenant-id" \
  "GRAPH_CLIENT_ID=your-graph-app-id" \
  "GRAPH_CLIENT_SECRET=your-graph-secret" \
  "GRAPH_TENANT_ID=your-tenant-id" \
  "HUMAN_OWNER_AAD_ID=human-owner-object-id" \
  "REVIEWER_TOKENS=alice@bank.com:reviewer:token1,bob@bank.com:checker:token2"
```

---

## Step 3 — Add GitHub secrets for the cron workflow

In your GitHub repo Settings → Secrets → Actions, add:

| Secret | Value |
|--------|-------|
| `REQSMITH_BASE_URL` | `https://ca-reqsmith-{SUFFIX}.{region}.azurecontainerapps.io` |
| `TICK_SECRET` | printed by azure-setup.sh |
| `ANTHROPIC_API_KEY` | your Anthropic key (for eval harness) |

The cron workflow `.github/workflows/cron-tick.yml` pings `/internal/tick` every 10 minutes during business hours, advancing the SLA escalation ladder.

---

## Step 4 — Register the Jira webhook

In Jira: **Settings → System → WebHooks → Create WebHook**

- URL: `https://YOUR_APP_URL/webhooks/jira`
- Events: Issue created, Issue updated, Comment created
- JQL filter: `project = BANK` (restrict to pilot project)
- Secret: value of `JIRA_WEBHOOK_SECRET`

---

## Step 5 — Register the Teams bot

> Requires Entra admin consent to sideload the app to your tenant.

1. In Azure Portal → Bot Services → your bot → Channels → Add Microsoft Teams
2. In Teams Admin Center → Teams apps → Manage apps → Upload → upload `deploy/teams-manifest.zip`
3. Or: use Teams Toolkit CLI to deploy the manifest

The bot endpoint is already set to `APP_URL/api/messages` by the setup script.

---

## Step 6 — Grant Graph daemon app consent

> Requires Entra Global Admin or Privileged Role Administrator.

Required scopes (app permissions, not delegated):

| Permission | Purpose |
|-----------|---------|
| `Calendars.ReadWrite` | `findMeetingTimes` + create events |
| `User.Read.All` | Resolve AAD user UPNs |
| `OnlineMeetingTranscript.Read.All` | M9 transcript ingestion *(optional — requires tenant application access policy)* |

In Azure Portal → App Registrations → your Graph app → API Permissions → Grant admin consent.

For `OnlineMeetingTranscript.Read.All`, also run:
```powershell
# Requires Teams PowerShell module
New-CsApplicationAccessPolicy -Identity reqsmith-transcript-policy `
  -AppIds @("YOUR_GRAPH_APP_ID") `
  -Description "reqsmith transcript ingestion"
Grant-CsApplicationAccessPolicy -PolicyName reqsmith-transcript-policy -Identity "ALL"
```
If this step is skipped, M9 transcript fetch returns `{"action": "unavailable"}` and the manual `.vtt` upload path is used instead — this is expected behaviour.

---

## Step 7 — Verify the deployment

```bash
# Health check
curl https://YOUR_APP_URL/healthz

# Manual tick (test the cron path)
curl -X POST https://YOUR_APP_URL/internal/tick

# Smoke test: post a Jira webhook payload
curl -X POST https://YOUR_APP_URL/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Atlassian-Token: no-check" \
  -d '{"webhookEvent":"jira:issue_created","issue":{"key":"BANK-1","fields":{"summary":"Test"}}}'
```

Check `GET /healthz` — the `last_tick` field should show a recent timestamp after your first cron run.

---

## Environment variable reference

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | ✓ | `postgresql+asyncpg://user:pass@host/db?ssl=require` |
| `JIRA_BASE_URL` | ✓ | `https://org.atlassian.net` |
| `JIRA_EMAIL` | ✓ | Service account email |
| `JIRA_API_TOKEN` | ✓ | Jira API token |
| `JIRA_WEBHOOK_SECRET` | ✓ | Validates incoming webhooks |
| `JIRA_PROJECT_KEY` | ✓ | Pilot project key (e.g. `BANK`) |
| `ANTHROPIC_API_KEY` | ✓ | LLM API key |
| `MODEL_DRAFTING` | — | Default: `claude-sonnet-4-6` |
| `MODEL_JUDGE` | — | Default: `claude-opus-4-8` (separate tier for independence) |
| `BOT_APP_ID` | ✓ | Azure Bot / Entra app ID |
| `BOT_APP_PASSWORD` | ✓ | Bot client secret |
| `TEAMS_TENANT_ID` | ✓ | Entra tenant ID |
| `GRAPH_CLIENT_ID` | ✓ | Graph daemon app client ID |
| `GRAPH_CLIENT_SECRET` | ✓ | Graph daemon app secret |
| `GRAPH_TENANT_ID` | ✓ | Same as TEAMS_TENANT_ID |
| `HUMAN_OWNER_AAD_ID` | ✓ | Object ID of the human owner (always invited to meetings) |
| `REVIEWER_TOKENS` | — | `email:role:token,...` for console auth; unset = dev mode |
| `OUTREACH_PAUSED` | — | `true` to pre-start with outreach paused |
| `PROMPT_PACK_VERSION` | — | Default: `v1` |
| `POLICY_PACK_VERSION` | — | Default: `v1` |
| `SERVER_PORT` | — | Default: `8000` |

---

## Neon free tier (DB fallback)

If Azure Postgres 12-month free period has expired, use [Neon](https://neon.tech) free tier:

1. Create a Neon project → copy the connection string
2. Set `DATABASE_URL=postgresql+asyncpg://user:pass@host.neon.tech/neondb?ssl=require`
3. Run migrations: `alembic upgrade head` (runs automatically on container start)

---

## Cost summary (steady state after free periods)

| Resource | SKU | Est. cost/month |
|----------|-----|-----------------|
| Container Apps | Consumption (min 0 replicas) | ~$0–5 |
| Container Registry | Basic | ~$5 |
| Postgres | B1ms (or Neon free) | $0–15 |
| Bot Service | F0 | $0 |
| GitHub Actions | Free tier (2000 min/mo) | $0 |
| **Total** | | **~$5–25/mo** |
