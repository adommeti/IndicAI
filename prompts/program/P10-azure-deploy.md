# P10-azure-deploy — Single-VM Azure POC deployment

<!-- Run: bash scripts/run-prompt.sh program P10-azure-deploy -->
Read CLAUDE.md, docs/prd-v2.md Parts B7/B8/E8 and infra/azure/README.md. Provide a reproducible Azure deployment for the POC that a person with Contributor rights on a subscription can run in under 30 minutes.

1. `infra/azure/main.bicep` (+ `main.bicepparam` example): one resource group; Standard_D4s_v5 Ubuntu 24.04 VM with a 128 GB premium disk, system-assigned identity, NSG allowing 443 from a parameter CIDR and 22 from a parameter CIDR only; Azure Database for PostgreSQL Flexible Server (B2s, PITR 14 days, private access) for comms_surveillance's append-only tables; a Key Vault holding `SARVAM_API_KEY`, `ANTHROPIC_API_KEY`, stack passwords and Langfuse secrets; a Storage account for backups; Log Analytics workspace with the VM agent. Tag everything `project=indic-ai-poc`.
2. `infra/azure/cloud-init.yaml`: installs Docker, uv, ffmpeg; clones the repo at a pinned tag; writes `.env` from Key Vault via managed identity (`az keyvault secret show` with the VM identity); runs `make up` and `make migrate`; installs Caddy as the TLS reverse proxy for the three app UIs and Langfuse/Grafana behind Entra ID (Caddy forward-auth to an oauth2-proxy container using the tenant's app registration; parameters documented).
3. `infra/azure/deploy.sh`: `az deployment group create`, then health checks against the public endpoints; `infra/azure/destroy.sh` with a confirmation prompt.
4. A GitHub Actions workflow `.github/workflows/deploy.yml` on `workflow_dispatch` with OIDC federated credentials (no stored cloud secrets), running Bicep lint/what-if on PRs that touch `infra/azure/` and the deployment on manual dispatch.
5. `infra/azure/README.md`: prerequisites, one-command deploy, cost table matching PRD B8/G4 at current list prices (cite the calculator export procedure), backup/restore, and the rollback procedure.
Acceptance: `az bicep build` and `bicep lint` pass in CI (add the lint step); `deploy.sh` has a `--what-if` mode that runs in CI without credentials being required for PRs (skips when absent); README complete; secrets never appear in the repo or the workflow logs.

## Execution notes
- No Azure credentials are available in the build session: validate with `az bicep build` (install the Azure CLI in the setup script or via `pip install azure-cli` in-session — prefer the standalone `bicep` binary from GitHub releases, which is on the network allowlist) and keep the live deployment as UNMEASURED with the exact command in the report.
- Reference current Microsoft Learn docs for Flexible Server private access, OIDC federation and cloud-init syntax; do not guess API versions.
