# OAuth compatibility: client acceptance and rollout

Scope of this change: OAuth discovery, optional user write consent, scope diagnostics,
and step-up signals. No new Viking API operations, changes to subscriptions, or live
portfolio operations are part of this work. The platform role remains an independent
permission boundary (Viking api.md, General provisions / Authorization).

## Supported server contract

- Existing `/mcp` URL, JSON Streamable HTTP, OAuth routes and token prefixes remain.
- `/mcp` still requires only `viking.read`. An already valid read-only connection is
  not rejected merely because the server offers portfolio controls.
- Both protected-resource metadata URLs (root and `/mcp` suffix) return the exact
  canonical resource and available scopes. Initial 401 has a resource metadata
  pointer without a forced read-only scope. This lets discovery-driven clients
  request the advertised scope set, while the human grants read only by default.
- DCR is supported for public (`none`) and confidential clients. Registrations that
  omit scope get the available scope set; **explicit** scope is not expanded.
  Offering/registering write is not consent and never upgrades an existing token.
- A combined scope request shows read/write choice on the server consent page.
  Read is default; write additionally requires `allow_portfolio_writes=yes`.
  A read-only authorization request cannot be upgraded by a forged form.
- A read-only registration made before this update may need one reconnect with
  new DCR. We deliberately do not mutate old registrations or widen refresh grants.
- Authorization and access-token verification reject a nonempty foreign resource;
  legacy tokens with no resource continue to work only on this issuer's MCP.
- Confirmed execution (`dry_run=false`, `confirm=true`) with a valid read-only token
  receives HTTP 403 + `WWW-Authenticate: Bearer error="insufficient_scope"` with both
  required scopes. Its MCP body still contains `isError` and
  `_meta["mcp/www_authenticate"]`. No command has been sent to Viking at this point.
- Direct tool-result errors also carry `mcp/www_authenticate`. Every tool descriptor
  declares `securitySchemes` and its `_meta` mirror. This implements the documented
  signals, **not proof that every ChatGPT/Claude surface handles them identically**.
- Disabled writes and platform/API rejection are NOT auth challenges. A timeout or
  unknown write outcome must never trigger automatic replay or auth retry.
- `get_authorization_status` reports effective scope and MCP write availability;
  it does not contact Viking and cannot certify platform role/portfolio permissions.

## Existing Lovable / K1FORGE contract

Based on the inspected `src/lib/viking.server.ts` flow: DCR and authorize carry an
explicit read or read+write scope, followed by POST of email/api_key/role/session
and optional `allow_portfolio_writes=yes`; authorization code is exchanged via
`client_secret_post`, and refresh is rotated. The new form's `access_mode` is
optional for this old programmatic POST. Regression tests reproduce this HTTP flow
with synthetic credentials and mocked Viking authorization, for read and write.

The Lovable project itself is NOT modified in this PR. Potential follow-ups:

1. Treat a write-only HTTP 403 `insufficient_scope` as a request to reconnect with
   additional scope, not as loss of the whole read source. Expose the challenge to
   the authenticated browser host; never to untrusted workspace code with tokens.
2. Use returned `token.scope` / `get_authorization_status`, not only locally cached
   scope. Explicit server-side downgrade makes old tokens effectively read only.
3. A future switch from programmatic credentials POST to user-facing consent is a
   separate client task, not a hidden prerequisite of this server update.

The old successful tool schemas/results and read/subscription paths are unchanged.
The unauthorized-write response changes from HTTP 200+tool error to HTTP 403+tool
error. This intentional error-path change must not be described as zero client impact.

## Permission reduction and persistence

An explicit new-form `access_mode=read` increments a durable generation for the
(client_id, subject) pair, where subject is the existing hash of Viking email+role.
Old access tokens keep reading but lose write; old refresh cannot regain write.
Fresh write consent can grant a new token without re-enabling old tokens. A code
created before downgrade cannot mint a write token after downgrade.

`oauth-grants.sqlite3` lives beside the existing OAuth client store on the persistent
volume, mode 0600. It contains hashed identities/counters only, no email, API key,
bearer token or client secret. Do not delete it or rotate the encryption key during
rollout. Read errors fail closed for write. Session credentials/tokens remain in RAM:
a container restart still expires those sessions; this change does not fix that.

Revocation scope is one registered client + user + role, not all connections of an
account. If a client recreates its DCR client_id, the new id is a distinct grant;
a new login cannot revoke an inaccessible old client_id. This is NOT a global
account-wide session-management UI. Different roles likewise remain separate.
The store is designed for the current single-replica persistent volume. Separate
replicas with independent disks do not share revocations; shared storage or a
central database is required before scaling. Rollback to old code that ignores
generations would also ignore new write revocations: disable server writes before
such rollback; don't erase the store, and review affected grants before re-enabling.

## Acceptance matrix

Do not check a live cell merely because a protocol simulation or another client passed.

| Client / scenario | Automated protocol regression | Real client acceptance |
|---|---|---|
| Discovery-driven hosted client (Claude-style DCR, advertised scopes) | Included | Pending Claude.ai browser test |
| Claude Desktop / Cowork remote connector | Same DCR contract | Pending, not inferred from Claude.ai |
| Claude Code / Codex explicit scopes, loopback PKCE | Public DCR / S256 / code exchange included | Pending on installed clients |
| ChatGPT tool metadata + auth error metadata | Descriptor + direct result + HTTP challenge included | Pending ChatGPT linking UI and step-up test |
| Lovable legacy read and write form POST + refresh | Exact HTTP argument shape reproduced | Pending unchanged client smoke |
| Existing read-only tokens and explicit read-only registrations | Included | Verify live read/subscription before rollout |
| Downgrade and isolation from other clients/users | Included | Pending same-connection UI test |

CIMD, upstream enterprise SSO, arbitrary third-party IdPs, session persistence across
replicas and full account-wide revocation are not newly implemented or advertised.
DCR is the current interoperability route. No claim of universal compatibility.

## Rollout gate

1. Run locked Python 3.11 / 3.12 lint + full regression suite; record exact tested SHA.
2. Deploy only after the user's separate approval. Preserve volume and token keys.
3. Check metadata, unauthenticated 401 and CORS without a Viking login.
4. In Claude.ai, re-add once if old registration was read-only; verify visible access
   choice, choose read, verify read; reconnect with write consent and verify scope
   with `get_authorization_status` before any real write.
5. Test unchanged Lovable read, reconnect, refresh and subscription lifecycle. Test
   its write auth on a separately approved test portfolio, not on a live strategy.
6. Complete other client cells separately. Live trading mutation is never implied
   by approval of a deployment or OAuth check.

## Primary references (checked 2026-09-23)

- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization): scope selection, 401/403, PKCE and resource binding.
- [Claude connector authentication](https://claude.com/docs/connectors/building/authentication): initial challenge precedence, DCR, metadata and supported token auth.
- [OpenAI plugin authentication](https://developers.openai.com/plugins/build/auth): per-tool securitySchemes and mcp/www_authenticate.
- [Viking API](https://github.com/fkviking/bot-doc/blob/master/assets/ru/api.md): platform role remains independently enforced.
