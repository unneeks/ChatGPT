#!/usr/bin/env bash
# Azure free-tier provisioning for reqsmith.
# Run ONCE from a shell with az CLI logged in (az login).
# Prerequisites: az CLI >= 2.60, Docker (for image build+push).
#
# Usage:
#   export REQSMITH_SUFFIX=mybank   # unique suffix for resource names
#   bash deploy/azure-setup.sh
#
# What this creates (all within free-tier grants where noted):
#   Resource Group:       rg-reqsmith-{SUFFIX}
#   Container Registry:   crreqsmith{SUFFIX}  (Basic SKU ~$5/mo — cheapest option)
#   Container Apps Env:   cae-reqsmith-{SUFFIX} (Consumption plan — free grant)
#   Container App:        ca-reqsmith-{SUFFIX}  (min replicas=0, scale-to-zero)
#   Postgres Flexible:    psql-reqsmith-{SUFFIX} (B1ms, 12-month free for new accounts)
#   Bot Service:          bot-reqsmith-{SUFFIX}  (F0 SKU — free)
#
# After running, follow the manual steps in deploy/README-azure.md to:
#   - Set secrets (Jira, Anthropic, Graph, Teams)
#   - Register the Teams app manifest
#   - Grant Graph API admin consent
#   - Add REQSMITH_BASE_URL + TICK_SECRET to GitHub secrets for the cron workflow

set -euo pipefail

SUFFIX="${REQSMITH_SUFFIX:?Set REQSMITH_SUFFIX to a short unique identifier, e.g. mybank}"
LOCATION="${AZURE_LOCATION:-eastus}"
RG="rg-reqsmith-${SUFFIX}"
ACR="crreqsmith${SUFFIX}"
CAE="cae-reqsmith-${SUFFIX}"
CA="ca-reqsmith-${SUFFIX}"
PSQL="psql-reqsmith-${SUFFIX}"
BOT="bot-reqsmith-${SUFFIX}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

echo "=== reqsmith Azure provisioning ==="
echo "Suffix:   $SUFFIX"
echo "Location: $LOCATION"
echo "RG:       $RG"

# ---------------------------------------------------------------------------
# Resource group
# ---------------------------------------------------------------------------
echo -e "\n[1/7] Creating resource group..."
az group create --name "$RG" --location "$LOCATION" --output none

# ---------------------------------------------------------------------------
# Container Registry (Basic — needed to push the image)
# ---------------------------------------------------------------------------
echo -e "\n[2/7] Creating Container Registry..."
az acr create --resource-group "$RG" --name "$ACR" \
  --sku Basic --admin-enabled true --output none

ACR_SERVER="${ACR}.azurecr.io"
ACR_PASSWORD=$(az acr credential show --name "$ACR" --query "passwords[0].value" -o tsv)

echo "Building and pushing Docker image..."
cd "$(dirname "$0")/.."   # go to reqsmith/
docker build -f deploy/Dockerfile -t "${ACR_SERVER}/reqsmith:${IMAGE_TAG}" .
docker login "$ACR_SERVER" --username "$ACR" --password "$ACR_PASSWORD"
docker push "${ACR_SERVER}/reqsmith:${IMAGE_TAG}"
cd -

# ---------------------------------------------------------------------------
# Postgres Flexible Server (B1ms — 12-month free for new accounts)
# ---------------------------------------------------------------------------
echo -e "\n[3/7] Creating Postgres Flexible Server..."
DB_ADMIN="reqsmithadmin"
DB_PASSWORD=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))")
echo "DB_PASSWORD: $DB_PASSWORD  <-- save this!"

az postgres flexible-server create \
  --resource-group "$RG" \
  --name "$PSQL" \
  --location "$LOCATION" \
  --admin-user "$DB_ADMIN" \
  --admin-password "$DB_PASSWORD" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --storage-size 32 \
  --version 15 \
  --output none

az postgres flexible-server firewall-rule create \
  --resource-group "$RG" \
  --name "$PSQL" \
  --rule-name allow-azure-internal \
  --start-ip-address 0.0.0.0 \
  --end-ip-address 0.0.0.0 \
  --output none

DB_HOST="${PSQL}.postgres.database.azure.com"
DATABASE_URL="postgresql+asyncpg://${DB_ADMIN}:${DB_PASSWORD}@${DB_HOST}/reqsmith?ssl=require"

# ---------------------------------------------------------------------------
# Container Apps environment
# ---------------------------------------------------------------------------
echo -e "\n[4/7] Creating Container Apps environment..."
az containerapp env create \
  --name "$CAE" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --output none

# ---------------------------------------------------------------------------
# Container App (scale-to-zero; free consumption grant)
# ---------------------------------------------------------------------------
echo -e "\n[5/7] Creating Container App..."
TICK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "TICK_SECRET: $TICK_SECRET  <-- add to GitHub secrets"

az containerapp create \
  --name "$CA" \
  --resource-group "$RG" \
  --environment "$CAE" \
  --image "${ACR_SERVER}/reqsmith:${IMAGE_TAG}" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR" \
  --registry-password "$ACR_PASSWORD" \
  --target-port 8000 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 3 \
  --cpu 0.5 \
  --memory 1.0Gi \
  --env-vars \
    "DATABASE_URL=${DATABASE_URL}" \
    "SERVER_PORT=8000" \
  --output none

APP_URL=$(az containerapp show \
  --name "$CA" --resource-group "$RG" \
  --query "properties.configuration.ingress.fqdn" -o tsv)
APP_URL="https://${APP_URL}"

echo -e "\n[6/7] Creating Bot Service (F0 — free)..."
az bot create \
  --resource-group "$RG" \
  --name "$BOT" \
  --kind registration \
  --sku F0 \
  --endpoint "${APP_URL}/api/messages" \
  --output none 2>/dev/null || echo "Bot Service creation requires existing Entra app — see README"

echo -e "\n[7/7] Done!"
echo ""
echo "============================================================"
echo "App URL:         $APP_URL"
echo "TICK_SECRET:     $TICK_SECRET"
echo "DB_PASSWORD:     $DB_PASSWORD"
echo "ACR:             $ACR_SERVER"
echo "============================================================"
echo ""
echo "NEXT STEPS (see deploy/README-azure.md):"
echo "  1. Set secrets on the Container App (Jira, Anthropic, Graph, Teams, HUMAN_OWNER_AAD_ID)"
echo "  2. Add REQSMITH_BASE_URL=$APP_URL and TICK_SECRET to GitHub repo secrets"
echo "  3. Register Teams app manifest (admin consent required)"
echo "  4. Grant Graph daemon app admin consent for Calendars.ReadWrite, User.Read.All"
echo "  5. Run: az containerapp update --name $CA --resource-group $RG \\"
echo "       --set-env-vars JIRA_BASE_URL=... ANTHROPIC_API_KEY=... etc"
echo "  6. Verify: curl ${APP_URL}/healthz"
