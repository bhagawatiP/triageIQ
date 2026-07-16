# Atlassian API Clients

Shared HTTP client modules for authenticating and communicating with Jira REST API and Xray Cloud GraphQL API. These are library modules — not standalone scripts — imported by other skill scripts via `sys.path`.

## Modules

### jira_client.py — Jira REST API Client

Provides Basic Auth HTTP helpers for the Jira REST API v2.

**Environment Variables Required:**
| Variable | Purpose |
|----------|---------|
| `CONFLUENCE_USERNAME` | Atlassian user email |
| `CONFLUENCE_TOKEN` | Atlassian API token |

**Key export:** `jira_request(method, path, **kwargs)` — authenticated Jira REST call.

**Default base URL:** `https://nice-ce-cxone-prod.atlassian.net`  
Override with `JIRA_BASE_URL` environment variable.

---

### xray_client.py — Xray Cloud GraphQL Client

Provides token-cached GraphQL client for the Xray Cloud API.

**Environment Variables Required:**
| Variable | Purpose |
|----------|---------|
| `XRAY_CLIENT_ID` | Xray Cloud API client ID |
| `XRAY_CLIENT_SECRET` | Xray Cloud API client secret |

**Key export:** `xray_graphql(query, variables)` — authenticated Xray Cloud GraphQL call.

**Xray endpoint:** `https://xray.cloud.getxray.app/api/v2/graphql`

---

## Usage by Other Skills

Other skill scripts import these clients by inserting the `skills/atlassian-api-clients/scripts/` directory into `sys.path`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'atlassian-api-clients', 'scripts'))
from xray_client import xray_graphql
```

Skills that depend on these clients:
- `xray-add-tests-to-plan` — uses `xray_client`
- `xray-create-test` — uses `xray_client`
- `xray-create-test-plan` — uses `xray_client`
- `xray-get-testplan-tests` — uses `xray_client`
