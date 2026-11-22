# Google Tag Manager MCP (Python)

An internal-use MCP server for managing Google Tag Manager through the Tag Manager API v2.
It uses your Google user identity by default, so it can access the GTM accounts already shared
with you. A service account can be configured as a fallback.

The server defaults to **read-only** access (`GTM_ACCESS_MODE=read`) and exposes account/container
discovery, container version summaries/entity filtering/diffing/export, workspace and entity reads,
environments, and destinations. Enabling `write` or `publish` mode additionally
exposes workspace isolation and synchronization, CRUD for tags/triggers/variables/folders, built-in
variables, version creation, and guarded publishing. It uses the raw GTM API JSON shape for entity
bodies so it does not hide advanced GTM options.

## Requirements

- Python 3.10+
- A Google Cloud project with **Tag Manager API** enabled
- For user authentication: an OAuth 2.0 **Desktop app** client
- For fallback authentication: a service account that has been added as a GTM user

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Access modes

Access is controlled by `GTM_ACCESS_MODE` (`read`, `write`, or `publish`). When unset, it defaults
to `read`; any unrecognized value fails closed at startup.

| Mode | Tools exposed | OAuth scopes requested |
|---|---|---|
| `read` (default) | Read-only discovery, diagnostics, version summaries/entities/diffs/exports, workspace & entity reads, environments, destinations, inventory | `https://www.googleapis.com/auth/tagmanager.readonly` |
| `write` | All `read` tools plus workspace creation/sync, entity create/update/delete, built-in variable enable/disable, and version creation | `https://www.googleapis.com/auth/tagmanager.readonly`, `https://www.googleapis.com/auth/tagmanager.edit.containers`, `https://www.googleapis.com/auth/tagmanager.edit.containerversions` |
| `publish` | All `read` and `write` tools plus `publish_version` (still requires `confirm=true`) | `https://www.googleapis.com/auth/tagmanager.readonly`, `https://www.googleapis.com/auth/tagmanager.edit.containers`, `https://www.googleapis.com/auth/tagmanager.edit.containerversions`, `https://www.googleapis.com/auth/tagmanager.publish` |

Account-management and user-management scopes (`tagmanager.manage.accounts`,
`tagmanager.manage.users`) are never requested. User permissions are intentionally not exposed
because reading them requires the `tagmanager.manage.users` scope.

If you change `GTM_ACCESS_MODE` (for example from `read` to `write` or `publish`), you must re-run
`.venv/bin/tag-manager-auth` so the saved OAuth token includes the required scopes, then restart
your MCP client.

## Preferred authentication: your Google account

1. In Google Cloud Console, enable the Tag Manager API.
2. Configure the OAuth consent screen. For internal use, choose **Internal** when your Google
   Workspace organization permits it; otherwise add your account as a test user.
3. Create an OAuth client ID with application type **Desktop app** and download its JSON file.
4. Set the access mode (optional, defaults to `read`) and credential paths, then authorize once:

```bash
export GTM_ACCESS_MODE=read
export GTM_AUTH_MODE=user
export GTM_OAUTH_CLIENT_SECRETS=/absolute/path/to/client_secret.json
export GTM_OAUTH_TOKEN_FILE="$HOME/.config/gtm-mcp/token.json"
.venv/bin/tag-manager-auth
```

The local callback binds only to `127.0.0.1`. The refresh token is written with mode `0600`.
Do not put either credential file in this repository.

After the first authorization, `GTM_OAUTH_CLIENT_SECRETS` is only needed if the saved grant is
removed or revoked. Keep `GTM_OAUTH_TOKEN_FILE` configured in the MCP client environment.

## Service-account fallback

Add the service account email under **Admin > Account User Management** or **Container User
Management** in GTM, then configure one of:

```bash
export GTM_AUTH_MODE=service-account
export GTM_SERVICE_ACCOUNT_FILE=/absolute/path/to/service-account.json
```

or, for a secret-injected environment:

```bash
export GTM_SERVICE_ACCOUNT_JSON='{"type":"service_account", ...}'
```

With `GTM_AUTH_MODE=auto` (the default), resolution order is:

1. Existing user OAuth token (or interactive OAuth when explicitly run through `tag-manager-auth`)
2. `GTM_SERVICE_ACCOUNT_JSON`
3. `GTM_SERVICE_ACCOUNT_FILE` / `GOOGLE_APPLICATION_CREDENTIALS`

Application Default Credentials are also supported explicitly with `GTM_AUTH_MODE=adc`.

## MCP client configuration

Run `pwd` and replace `/absolute/path/to/tag-manager-mcp` below.

```json
{
  "mcpServers": {
    "tag-manager-local": {
      "command": "/absolute/path/to/tag-manager-mcp/.venv/bin/tag-manager-mcp",
      "args": [],
      "env": {
        "GTM_ACCESS_MODE": "read",
        "GTM_AUTH_MODE": "auto",
        "GTM_OAUTH_TOKEN_FILE": "/Users/you/.config/gtm-mcp/token.json",
        "GTM_SERVICE_ACCOUNT_FILE": "/optional/fallback/service-account.json"
      }
    }
  }
}
```

The server uses stdio transport. Logs go to stderr and will not corrupt MCP messages.

## Bulk export

`tag-manager-export` exports live container versions as standard GTM v2 JSON files
(`<publicId>_v<containerVersionId>.json`). It always runs in `read` mode only (never requiring
write scopes) and uses the shared quota guardrails.

```bash
.venv/bin/tag-manager-export
.venv/bin/tag-manager-export --out ./exports --usage-context all
.venv/bin/tag-manager-export --container GTM-W525XW4 --container GTM-OTHER1
```

Options:

- `--out DIR`: Output directory (defaults to `GTM_EXPORT_DIR` or `~/.local/share/gtm-mcp/exports`).
- `--usage-context CONTEXT`: Container usage context filter (default `web`; pass `all` for no filter).
- `--container PUBLIC_ID`: Repeatable filter to export only specific public container IDs.

Exit codes: `0` when at least one container was exported and none failed, `1` when any container
failed, fetching failed, or none matched, `2` on authentication or configuration error
(`AuthenticationError` or invalid `GTM_ACCESS_MODE`).

## Quota guardrails

Google publishes a limit of 10,000 requests per project per day and 0.25 QPS, enforced as 25
requests in a rolling 100-second window. The server therefore defaults to:

- At least 4.1 seconds between API requests, coordinated across local MCP processes.
- A persistent 9,000-request daily safety budget, leaving 10% headroom.
- Up to three retries for `429` and quota-related `403` responses, using `Retry-After` when
  supplied or exponential backoff with jitter otherwise.
- No automatic retry for non-quota failures, avoiding accidental duplicate writes.

State is stored at `~/.config/gtm-mcp/quota-state.json`. Inspect it through the zero-cost
`quota_status` MCP tool. Configure the behavior with `GTM_MIN_REQUEST_INTERVAL_SECONDS`,
`GTM_DAILY_REQUEST_BUDGET`, `GTM_QUOTA_MAX_RETRIES`, `GTM_QUOTA_BACKOFF_SECONDS`, and
`GTM_QUOTA_STATE_FILE`.

## SQLite debug log

Every MCP tool invocation and outbound GTM API attempt is written to `debug.db` in the project
root by the supplied `.env` configuration. Related MCP and API rows share a `call_id`. Rows include
sanitized inputs and outputs, request method/resource, duration, HTTP status, retry attempt, process
ID, and error details. OAuth tokens, authorization values, client secrets, private keys, refresh
tokens, and credential objects are redacted. Payloads are capped at 256 KiB per field.

The database uses WAL mode for concurrent Codex and Claude Desktop processes and automatically
removes rows older than 30 days. Use `debug_log_status` and `debug_log_recent` to inspect it through
MCP, or open the database directly with SQLite. Logging is best-effort and cannot block GTM calls.

Configure it with `GTM_DEBUG_LOG_ENABLED`, `GTM_DEBUG_DB_PATH`, `GTM_DEBUG_RETENTION_DAYS`, and
`GTM_DEBUG_MAX_PAYLOAD_BYTES`.

## Tools

### `read` level

| Tool | Purpose |
|---|---|
| `auth_status` | Verify credentials and report active access mode, scopes, quota, and accounts |
| `quota_status` | Return local GTM request budget and rate-limit settings without API calls |
| `debug_log_status` | Return SQLite debug-log status and event counts |
| `debug_log_recent` | Return recent sanitized debug events (`limit` 1-500) |
| `list_accounts` | Discover accessible GTM accounts |
| `list_containers` | List containers in a GTM account |
| `get_container` | Get one GTM container |
| `resolve_container_identifier` | Resolve `GTM-*`/destination IDs to account and container IDs |
| `list_workspaces` | List workspaces in a GTM container |
| `get_workspace_status` | Check pending changes and merge conflicts in a workspace |
| `list_entities` | List `tags`, `triggers`, `variables`, `folders`, `templates`, `clients`, `transformations`, `zones`, or `gtag_config` in a workspace |
| `get_entity` | Get one workspace entity by ID for any of the `list_entities` kinds |
| `list_builtin_variables` | List enabled built-in variables in a workspace |
| `list_versions` | List version headers for a GTM container |
| `get_version_summary` | Summarize entity counts and paused tags for the live or given version |
| `get_version_entities` | Return entities of a `ContainerVersion` key (`tag`, `trigger`, `variable`, `folder`, `builtInVariable`, `customTemplate`, `client`, `zone`, `transformation`, `gtagConfig`) with optional `name_contains` filter |
| `diff_container_versions` | Diff two container versions (or `from_version_id` against the live version) |
| `export_container_to_file` | Write the full GTM v2 export JSON into `GTM_EXPORT_DIR` and return path and summary |
| `list_environments` | List environments in a GTM container |
| `list_destinations` | List destinations linked to a GTM container |
| `inventory` | Inventory every accessible account and its containers without aborting on errors |
| `list_live_versions` | Summarize live versions across accessible containers, optionally saving export files |

### `write` level

| Tool | Purpose |
|---|---|
| `create_workspace` | Create an isolated GTM workspace for changes |
| `sync_workspace` | Synchronize a workspace with the latest container version and report conflicts |
| `create_entity` | Create a `tags`, `triggers`, `variables`, or `folders` resource from API JSON |
| `update_entity` | Update a `tags`, `triggers`, `variables`, or `folders` resource (supply `fingerprint`) |
| `delete_entity` | Delete a `tags`, `triggers`, `variables`, or `folders` resource; requires `confirm=true` |
| `enable_builtin_variables` | Enable one or more GTM built-in variable types |
| `disable_builtin_variables` | Disable GTM built-in variable types; requires `confirm=true` |
| `create_version` | Create an unpublished container version after conflict/change checks |

### `publish` level

| Tool | Purpose |
|---|---|
| `publish_version` | Publish a container version live; requires `confirm=true` |

Always call `list_workspaces`; do not assume the workspace ID is `1`. For updates, first read the
entity and pass its `fingerprint` to prevent overwriting a concurrent edit.

When only a public identifier is known, call `resolve_container_identifier("GTM-W525XW4")`. It
uses Google's direct container lookup endpoint and returns `account_id` plus `container_id` without
scanning every accessible account. The server instructions explicitly direct MCP clients to use
this resolution workflow automatically.

### Example tag body

`create_entity(kind="tags", ...)` accepts the documented GTM v2 tag resource body:

```json
{
  "name": "GA4 - generate_lead",
  "type": "gaawe",
  "parameter": [
    {"type": "template", "key": "eventName", "value": "generate_lead"},
    {"type": "tagReference", "key": "measurementId", "value": "{{GA4 Measurement ID}}"}
  ],
  "firingTriggerId": ["42"]
}
```

GTM tag and parameter type codes vary by template. Read a comparable existing entity first or use
the Tag Manager API reference instead of guessing the payload.

## Development

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

## Credits

Forked from AdGurus/tag-manager-mcp (MIT).

## Design references

This implementation was informed by:

- [neep305/mcp-for-gtm](https://github.com/neep305/mcp-for-gtm), particularly its Python,
  FastMCP, and installed-app OAuth approach.
- [paolobietolini/gtm-mcp-server](https://github.com/paolobietolini/gtm-mcp-server), particularly
  its wider API surface, service-account mode, workspace conflict checks, and explicit confirmation
  before publishing.

The code in this repository is a new Python implementation rather than a mechanical port.
