# Google Tag Manager MCP (Python)

An internal-use MCP server for managing Google Tag Manager through the Tag Manager API v2.
It uses your Google user identity by default, so it can access the GTM accounts already shared
with you. A service account can be configured as a fallback.

The server defaults to **read-only** access (`GTM_ACCESS_MODE=read`) and exposes account/container
discovery, container version summaries/entity filtering/diffing/export, workspace and entity reads,
environments, destinations, and import dry-run planning. Higher modes (`write`, `publish`, `admin`)
progressively expose workspace and entity editing (including templates, clients, transformations,
zones, and gtag configs), container export import, two-step human-approved publishing, and
account/container/user-permission administration. It uses the raw GTM API JSON shape for entity
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

Access is controlled by `GTM_ACCESS_MODE` (`read`, `write`, `publish`, or `admin`). When unset, it
defaults to `read`; any unrecognized value fails closed at startup.

| Mode | Tools exposed | OAuth scopes requested |
|---|---|---|
| `read` (default) | Discovery, diagnostics, version headers/summaries/entities/diffs/exports, workspace/folder/entity/environment/destination/snippet/account reads, inventory, import planning | `https://www.googleapis.com/auth/tagmanager.readonly` |
| `write` | All `read` tools plus workspace creation/update/sync/conflict-resolution/quick-preview/bulk-update, entity CRUD and revert (all workspace entity kinds), built-in variables, gallery template import, folder moves, version metadata/delete/undelete, environment metadata create/update/delete (without `containerVersionId`), destination linking, import apply | `https://www.googleapis.com/auth/tagmanager.readonly`, `https://www.googleapis.com/auth/tagmanager.edit.containers`, `https://www.googleapis.com/auth/tagmanager.edit.containerversions` |
| `publish` | All `read` and `write` tools plus `prepare_publish`, `publish_version`, `publish_to_environment`, `set_latest_version` (all require two in-client elicitation approvals or terminal approval + `confirm=true`), `list_publish_requests`, and `reauthorize_environment` | `https://www.googleapis.com/auth/tagmanager.readonly`, `https://www.googleapis.com/auth/tagmanager.edit.containers`, `https://www.googleapis.com/auth/tagmanager.edit.containerversions`, `https://www.googleapis.com/auth/tagmanager.publish` |
| `admin` | All `read`, `write`, and `publish` tools plus `delete_workspace`, container creation/update/deletion/combine/move-tag-id, `update_account`, and user-permission management (`list`/`get`/`create`/`update`/`delete`) | `https://www.googleapis.com/auth/tagmanager.readonly`, `https://www.googleapis.com/auth/tagmanager.edit.containers`, `https://www.googleapis.com/auth/tagmanager.edit.containerversions`, `https://www.googleapis.com/auth/tagmanager.publish`, `https://www.googleapis.com/auth/tagmanager.delete.containers`, `https://www.googleapis.com/auth/tagmanager.manage.accounts`, `https://www.googleapis.com/auth/tagmanager.manage.users` |

Note: `delete_workspace` is gated at `admin` because Google's Tag Manager API v2 discovery document
requires the `tagmanager.delete.containers` scope for `workspaces.delete`. Container lifecycle
operations (`create_container`, `update_container`, `delete_container`, `combine_containers`,
`move_tag_id`), `update_account`, and all user-permission tools (including `list_user_permissions`
and `get_user_permission`) are also intentionally restricted to `admin` mode.

If you change `GTM_ACCESS_MODE` (for example from `read` to `write`, `publish`, or `admin`), you
must re-run `.venv/bin/tag-manager-auth` so the saved OAuth token includes the required scopes,
then restart your MCP client.

## Publishing requires two approvals

Changing which container version **any** GTM environment serves — the live environment, a
user-created environment, or the `latest` environment — requires `GTM_ACCESS_MODE=publish`
(or `admin`) **and** two separate human confirmations inside your MCP client via MCP elicitation:

1. **Prepare via MCP**: The MCP client calls
   `prepare_publish(account_id, container_id, version_id, environment_id=None, latest=False)`.
   - Default (`environment_id=None, latest=False`): targets the `live` environment and diffs against
     the current live version.
   - `environment_id="<id>"`: targets `"environment:<id>"` and diffs against that environment's
     current `containerVersionId`.
   - `latest=True`: targets `"latest"` and diffs against the current latest version header.
   The server writes a **pending** request to `~/.config/gtm-mcp/publish-approvals.json`
   (override with `GTM_PUBLISH_APPROVAL_FILE`) and returns an `approval_id`, target, diff summary,
   expiry timestamp, and instructions.
2. **Confirm twice in your MCP client and publish**: The MCP client calls the matching publish tool
   with `approval_id` and `confirm=true`:
   - `publish_version(account_id, container_id, version_id, approval_id, confirm=true)` for `live`
   - `publish_to_environment(account_id, container_id, environment_id, version_id, approval_id, confirm=true)` for a user environment
   - `set_latest_version(account_id, container_id, version_id, approval_id, confirm=true)` for `latest`
   The server verifies the request is pending, unexpired, and matches the target, checks that the
   connected MCP client declared elicitation support (failing closed if not), and sends two
   separate elicitation prompts:
   - **Typed approval**: Shows the target (`LIVE`, `Environment <id> (<name>)`, or `LATEST`),
     container public ID and name, version ID and name, current version being replaced, and a
     sanitized diff summary (control characters escaped, long values truncated), and asks the human
     to type the exact container public ID (e.g. `GTM-W525XW4`).
   - **Click approval**: Shows a short final confirmation (`Publish <public_id> v<version> to
     <target> now?`) with a `publish` boolean defaulting to `false` that must be accepted as `true`.
   Any decline, cancel, mismatch, `publish=false`, or elicitation error publishes nothing and marks
   the request `cancelled` (a new `prepare_publish` is required). After both prompts succeed, the
   server records the approval, re-reads the target's currently served version from the GTM API,
   atomically consumes the single-use approval, and only then performs the publish.
3. **Terminal fallback for clients without elicitation**: The interactive terminal CLI
   `.venv/bin/tag-manager-approve <approval_id>` remains available as an alternative for MCP
   clients that do not support elicitation. A request approved in the terminal is accepted by the
   publish tools without prompting again **only if it was approved via the terminal**.

Safety properties of publish approvals:

- **10-minute expiry**: Approval requests expire after 600 seconds; expired requests are refused
  before any prompt is shown.
- **Single use and target binding**: Once consumed, the request is marked `used` and cannot be
  replayed. An approval prepared for one target (`live`, `latest`, or `environment:<id>`) cannot be
  consumed by another target. Creating a new request for the same account, container, and target
  cancels any prior open request for that target.
- **No tool-argument bypass**: Neither the typed container ID nor the click confirmation can be
  supplied as MCP tool arguments.
- **Current-version binding**: If the target's currently served version changes between
  `prepare_publish` and the publish call (including while the prompts are open), the approval is
  invalidated and publishing is refused.
- **Environment safeguards**: `create_environment` and `update_environment` accept only the exact
  camelCase metadata keys `name`, `description`, `url`, and `enableDebug` (all other keys,
  including `containerVersionId`, `container_version_id`, `workspaceId`, and `workspace_id`, are
  rejected). The only code path that may send `containerVersionId` for an environment is
  `publish_to_environment` after approval is consumed; there is no path that sends `workspaceId`
  for an environment. `reauthorize_environment` takes only `account_id`, `container_id`, and
  `environment_id` (no caller-supplied body). `list_environments` and `get_environment` redact
  `authorizationCode` (only `reauthorize_environment` returns the newly rotated code, and the debug
  log redacts it even inside JSON-encoded tool results).
- **Read-only inspection**: `list_publish_requests` lists open (`pending`/`approved`) requests
  without allowing state changes.

### Limitations

The approval prompts are shown by the MCP client; a client that auto-answers elicitations could
approve on its own, so use a client that shows them to you (Claude Code, Claude Desktop).

## Import a container export

You can inspect and import standard GTM v2 container export JSON files into a target workspace:

1. Place (or export via `export_container_to_file` / `tag-manager-export`) the JSON file inside
   `GTM_EXPORT_DIR` (defaults to `~/.local/share/gtm-mcp/exports`).
2. Call `plan_container_import(account_id, container_id, workspace_id, file_name, mode="merge", conflict="skip")`
   (`read` level) to dry-run the import against the workspace's existing folders, custom templates,
   variables, triggers, tags, and built-in variables. It returns planned creates, updates, deletes,
   skips, built-in variables to enable, warnings, and counts.
3. Call `apply_container_import(account_id, container_id, workspace_id, file_name, mode="merge", conflict="skip", confirm=False)`
   (`write` level) to recompute the plan and apply it in dependency order (remapping folder IDs,
   custom template `cvt_<containerId>_<templateId>` references, trigger IDs, and setup/teardown tag
   names).

Import options and guardrails:

- **Path confinement**: `file_name` must be a bare file name inside `GTM_EXPORT_DIR`. Path
  separators (`../`, subdirectories), absolute paths, and symlinks resolving outside
  `GTM_EXPORT_DIR` are rejected.
- **Modes**: `merge` (default) adds or resolves conflicts without deleting unrelated workspace
  entities; `overwrite` updates matching names and deletes workspace entities not in the export and
  requires `confirm=true`.
- **Conflict resolution** (in `merge` mode): `skip` (default), `overwrite`, or `rename` (appends
  `(imported)`).

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
- `--usage-context CONTEXT`: Container usage context filter (default `web`; pass `all` for no filter; ignored when `--container` is given).
- `--container PUBLIC_ID`: Repeatable public container ID to resolve and export directly without scanning all accounts.

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
| `get_account` | Get a GTM account by ID |
| `list_containers` | List containers in a GTM account |
| `get_container` | Get one GTM container |
| `get_container_snippet` | Fetch the tagging snippet for a container |
| `resolve_container_identifier` | Resolve `GTM-*`/destination IDs to account and container IDs |
| `list_workspaces` | List workspaces in a GTM container |
| `get_workspace` | Get one GTM workspace by ID |
| `get_workspace_status` | Check pending changes and merge conflicts in a workspace |
| `list_entities` | List `tags`, `triggers`, `variables`, `folders`, `templates`, `clients`, `transformations`, `zones`, or `gtag_config` in a workspace |
| `get_entity` | Get one workspace entity by ID for any of the `list_entities` kinds |
| `list_folder_entities` | List tags, triggers, and variables inside a workspace folder |
| `list_builtin_variables` | List enabled built-in variables in a workspace |
| `list_versions` | List version headers for a GTM container |
| `latest_version_header` | Fetch the latest `ContainerVersionHeader` for a container |
| `get_version_summary` | Summarize entity counts and paused tags for the live or given version |
| `get_version_entities` | Return entities of a `ContainerVersion` key (`tag`, `trigger`, `variable`, `folder`, `builtInVariable`, `customTemplate`, `client`, `zone`, `transformation`, `gtagConfig`) with optional `name_contains` filter |
| `diff_container_versions` | Diff two container versions (or `from_version_id` against the live version) |
| `export_container_to_file` | Write the full GTM v2 export JSON into `GTM_EXPORT_DIR` and return path and summary |
| `plan_container_import` | Dry-run importing a bare-filename export JSON from `GTM_EXPORT_DIR` into a workspace |
| `list_environments` | List environments in a GTM container (`authorizationCode` redacted) |
| `get_environment` | Get one environment by ID (`authorizationCode` redacted) |
| `list_destinations` | List destinations linked to a GTM container |
| `get_destination` | Get one destination linked to a container |
| `inventory` | Inventory every accessible account and its containers without aborting on errors |
| `list_live_versions` | Summarize live versions across accessible containers, optionally saving export files |

### `write` level

| Tool | Purpose |
|---|---|
| `create_workspace` | Create an isolated GTM workspace for changes |
| `update_workspace` | Update a workspace (supply `fingerprint`) |
| `sync_workspace` | Synchronize a workspace with the latest container version and report conflicts |
| `resolve_conflict` | Resolve a workspace merge conflict with an Entity payload; requires `confirm=true` |
| `quick_preview` | Quick-preview the current workspace |
| `bulk_update` | Apply multiple `ProposedChange` entity updates to a workspace in one request |
| `create_entity` | Create a `tags`, `triggers`, `variables`, `folders`, `templates`, `clients`, `transformations`, `zones`, or `gtag_config` resource from API JSON |
| `update_entity` | Update any workspace entity kind supported by `create_entity` (supply `fingerprint`) |
| `delete_entity` | Delete any workspace entity kind supported by `create_entity`; requires `confirm=true` |
| `revert_entity` | Revert uncommitted workspace changes to a tag, trigger, variable, folder, template, client, transformation, or zone |
| `import_template_from_gallery` | Import a Community Template Gallery custom template into a workspace |
| `move_entities_to_folder` | Move tags, triggers, and/or variables to a folder (`folder_id="0"` removes from folder) |
| `enable_builtin_variables` | Enable one or more GTM built-in variable types |
| `disable_builtin_variables` | Disable GTM built-in variable types; requires `confirm=true` |
| `revert_builtin_variable` | Revert uncommitted changes to a built-in variable in a workspace |
| `create_version` | Create an unpublished container version after conflict/change checks |
| `update_version` | Update a container version's metadata (supply `fingerprint`) |
| `delete_version` | Delete a container version; requires `confirm=true` |
| `undelete_version` | Restore a deleted container version |
| `create_environment` | Create an environment in a container (accepts only `name`, `description`, `url`, `enableDebug`) |
| `update_environment` | Update an environment's metadata (supply `fingerprint`; accepts only `name`, `description`, `url`, `enableDebug`) |
| `delete_environment` | Delete an environment; requires `confirm=true` |
| `link_destination` | Link a destination to a container |
| `apply_container_import` | Apply an export JSON file from `GTM_EXPORT_DIR` to a workspace; requires `confirm=true` when any entity would be updated or deleted |

### `publish` level

| Tool | Purpose |
|---|---|
| `prepare_publish` | Create a pending publish approval request with a diff summary against `live`, `environment_id`, or `latest` |
| `publish_version` | Publish a container version live after two-step in-client elicitation approval (or `tag-manager-approve` terminal fallback); requires `approval_id` and `confirm=true` |
| `publish_to_environment` | Point a user environment at a container version after two-step in-client elicitation approval (or `tag-manager-approve` terminal fallback); requires `approval_id` and `confirm=true` |
| `set_latest_version` | Set a container version as the latest version after two-step in-client elicitation approval (or `tag-manager-approve` terminal fallback); requires `approval_id` and `confirm=true` |
| `list_publish_requests` | List open (`pending` and `approved`) publish requests without modifying state |
| `reauthorize_environment` | Re-generate and return the authorization code for an environment |

### `admin` level

| Tool | Purpose |
|---|---|
| `delete_workspace` | Delete a workspace (`delete.containers` scope); requires `confirm=true` |
| `create_container` | Create a container in an account |
| `update_container` | Update a container (supply `fingerprint`) |
| `delete_container` | Delete a container; requires `confirm=true` |
| `combine_containers` | Combine another container into the target container; requires `confirm=true` |
| `move_tag_id` | Move a tag ID into a newly created container; requires `confirm=true` |
| `update_account` | Update a GTM account (supply `fingerprint`) |
| `list_user_permissions` | List user permissions on a GTM account |
| `get_user_permission` | Get a user permission by ID |
| `create_user_permission` | Create a user permission on an account |
| `update_user_permission` | Update an existing user permission on an account |
| `delete_user_permission` | Delete a user permission from an account; requires `confirm=true` |

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
