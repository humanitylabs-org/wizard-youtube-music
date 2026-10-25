# YouTube Music for Wizard

An unofficial music source for Wizard's Music app. Contract `wizard.music-source/1` remains compatible for search, radio and audio; optional account-library endpoints are additive. MIT licensed.

## Playback and home relay

At home, test playback directly. Cloud/VPS addresses may be blocked by YouTube; use [Wizard Home Relay](https://github.com/humanitylabs-org/wizard-home-relay) on an always-on home Mac, Windows or Linux computer if needed. Follow that project's instructions for your platform; this connector does not install or manage a relay. Keep both devices on your private Tailscale network, restrict access to the authenticated SOCKS service and never expose it publicly. Import its private pairing file only in this app's authenticated HTTPS file form, never in chat. A relay must remain awake and online. Existing valid relay settings are preserved by account setup.

**No guarantee of ad-free, unlimited or uninterrupted playback, Premium entitlements, or account safety.** A residential connection does not guarantee playback or reduced account risk. Respect YouTube's terms, rights and account entitlements. Catalog account authorization does not authenticate yt-dlp playback; no account tokens are handed to the downloader.

The setup screen includes **Help me set this up**: a copyable, non-secret request and the shell's supported `wizard:open-app` / `agents` navigation. The owner chooses a chat and sends the request. There is no claimed automatic message delivery, notification or reply subscription. Standalone/older shells fall back to manual navigation. No agent identity is guessed.

### Private file pairing (disabled until owner provisions access)

This implements the home relay's `PAIRING.md` export contract, not remote installation or an automatic cross-device pairing service. **Do not upload a relay export to an agent, chat, email or public link.**

1. Keep this connector behind Wizard's authenticated private **HTTPS** origin. Set `WIZARD_RELAY_IMPORT_ORIGIN` to its exact origin, for example `https://wizard.example` (no path or trailing slash). Preserve that exact `Host` through the reverse proxy. This HTTP backend relies on the trusted TLS-terminating proxy: keep its raw port loopback-only/private and never expose it publicly. `Origin` and forwarded headers are not authentication or proof of TLS; arbitrary proxy identity headers are not trusted here.
2. Independently provision `DATA_DIR/relay-import-key` as a regular, non-symlink file, owned by the app user, mode **0600**, in the private data directory (0700). Its content must be a random URL-safe 32–128 character authorization key. Generate it privately and retain it in the owner's password manager, transfer it using a trusted secret channel, never chat/argv/logs. The key is **separate** from the home relay token. If absent, incorrectly permissioned or with no configured HTTPS origin, relay operations fail closed with 503. No default password or generated credential is returned by the API.
3. On the home Mac/Windows/Linux machine, follow the reviewed relay install instructions and use `relay.py pair` to create a **new** export in its dedicated private pairing directory. File creation does not rotate the home token and this file is reusable secret material, not an expiring code. The owner must move/select that file through a private trusted path; there is no native file transfer/install automation here.
4. In this app's HTTPS page, enter the private import authorization key, select the pairing JSON file, and explicitly confirm that it belongs to the intended home Tailscale device with scoped tailnet access. Tap **Import privately**. The browser reads the file without displaying it and sends bounded JSON to the same-origin endpoint using an Authorization header; it does not save secrets in browser storage. File/key controls are cleared after an attempted import. Request/response bodies and Authorization headers must also be excluded from upstream proxy/access logs.
5. The server accepts only schema `wizard-home-relay-pairing-v1`, `scheme=socks5h`, `username=wizard`, `provider=youtube`, canonical numeric IPv4 in `100.64.0.0/10`, integer port 1–65535 (not bool), and URL-safe password length 32–128. No arbitrary schemes, hostnames, IPv6, loopback, LAN, public hosts or userinfo hosts. Export `allowed_domains` is informational and never expands policy. CGNAT membership is not device ownership proof; the explicit owner confirmation and scoped tailnet grants remain necessary.
6. Import saves the URL internally using atomic exclusive temporary files and mode 0600 `settings.json`. API/status/tools never return URL, password, import key or raw probe diagnostics. Import reports **imported_unverified**, not paired. Re-enter the authorization key and tap **Test relay connectivity**. This runs on the actual connector server, requires SOCKS username/password (no anonymous downgrade), sends remote DNS for fixed `music.youtube.com:443`, verifies TLS, then sends a fixed HEAD request. A 15-second total I/O deadline, bounded SOCKS fields and 256-byte HTTP status line cap apply. No redirects, response-body downloads, account cookies or file-controlled destinations are used. Only HTTP 200 produces **verified**; 403/429 is **provider_blocked**, all other failures are **failed**. A successful probe is not a playback guarantee.
7. **Test playback** is a separate real song download; **Verify connection** is a separate authenticated account-library test. Re-import/removal resets relay verification and playback. Remove locally requires the same import key and deletes only connector settings; rotate/revoke the reusable home token separately. After verified import, delete the export using the file manager; this is not guaranteed secure erasure on backups/SSDs. Back up connector settings as secret material. Previously stored non-Tailscale/invalid legacy URLs are no longer used; re-export/import a conforming relay.

Relay API (all JSON POST; exact configured HTTPS Origin and Host, no query parameters, same-origin fetch context, and `Authorization: Bearer <private import key>` required):
- `/api/relay/import`: `{pairing: <private export object>, confirm_home_device: true}` → redacted connector status. UI file cap 16 KiB; request cap 64 KiB.
- `/api/relay/test`: `{}` → redacted status after connectivity check.
- `/api/relay`: `{relay: ""}` → remove locally. Raw credential URL submission is deliberately refused.

`relay` remains a compatibility boolean meaning valid settings exist, not connectivity. Inspect additive `relay_status.state`, `at`, `message` and `import_enabled`. The persisted successful probe is historical evidence at `at`, not continuous monitoring. Existing catalog/playback/MCP contracts are unchanged. The import key is needed on each relay operation, intentionally not retained across actions.

## Optional own-account connection

Research sources (reviewed for this implementation):
- https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html
- https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html
- pinned `ytmusicapi==1.12.3` `OAuthCredentials` / token implementation.

The documented OAuth route requires **your own Google Cloud project**, YouTube Data API enabled, and an OAuth client of type **TVs and Limited Input devices**. This is the device flow documented by ytmusicapi, not a pre-registered Wizard "Sign in with Google" integration or a claim of official Google support for this unofficial client. Google policies, consent configuration, testing-user restrictions and verification can prevent authorization. Configure consent/test users in Google Cloud as applicable. We do not fall back to collecting browser cookies if Google refuses the flow.

1. On the owner's private server, provision `DATA_DIR/oauth-client.json` with exactly `client_id` and `client_secret` from that client. Use a private local editor or approved secret/file transfer, directory mode 0700 and file mode 0600; never include credentials in chat, shell arguments, screenshots, git, logs or support reports. Default container data directory is `/data`. This is an owner administration prerequisite, not a secret upload API.
2. Only expose the connector behind Wizard's authenticated same-origin private HTTPS or private loopback. Do not publish its raw HTTP port. The app relies on that owner-access boundary; it is not a multi-user authorization server. Cross-origin browser writes are refused, but this does not replace authentication. Back up the data volume as secret material.
3. Open **Connect account**. The server uses the library's OAuthCredentials to request a device code. The app shows Google's allowlisted verification URL and user code; complete sign-in and consent on Google, not in Wizard or chat.
4. Return and tap **I completed Google authorization**. Pending consent/slow-down and expired/denied flows are distinct. Server-enforced polling honors Google's interval; device codes stay in process memory only. Restart or disconnect cancels a pending flow.
5. Access/refresh tokens are atomically saved as `ytmusic-oauth.json`, mode 0600, only after a token response. **Connected** is shown only after a successful authenticated library request, never simply because a credential file exists. After restart credentials are **unverified** until checked. Verify connection can report **needs attention** without revealing provider errors or tokens.
6. Disconnect deletes local OAuth and legacy browser credentials and cancels pending setup. Also revoke consent in your [Google Account connections](https://myaccount.google.com/connections). Local deletion is not remote revocation. Legacy `ytmusic-browser.json` is recognized but never requested through chat or a cookie upload screen.

Library access may reveal playlists/likes to the authenticated owner of this Wizard. This implementation is single-owner: no cross-user account separation. It does not import general YouTube settings or automatically train Music preferences.

## API for Music

Existing routes: `GET /api/status`, `/api/music/search?q=`, `/api/music/radio?seed=`, `/api/music/related-artists?name=`, `/api/music/genre?genre=`, `/api/music/audio/<videoId>` (Range supported).

Additive account routes (JSON POST, no secrets/codes in request bodies):
- `/api/account/start` → `verification_url`, `user_code`, `expires_in` (owner only; never forward to an agent).
- `/api/account/poll` → `pending` + `retry_after`, `expired`, `denied`, or `complete` + `status`. Complete means tokens received; inspect `status.account_status.state` for authenticated library verification.
- `/api/account/check` and `/api/account/disconnect` → connector status.

`GET /api/status` preserves `account` as a boolean, now true only for verified access. `account_status.state` is `signed_out`, `unverified`, `connected`, or `needs_attention`; `checked_at` reports the last check. Account status and playback readiness are independent. The status never returns credentials or device codes.

Additive library routes, advertised in status capabilities:
- `GET /api/music/library/playlists?limit=40` → `{playlists: [{playlistId,title,thumbnails,count}]}` (only fields provided by YouTube).
- `GET /api/music/library/likes?limit=40` → `{tracks: [...]}` using the existing sanitized track shape.
- `GET /api/music/library/playlist?id=<playlistId>&limit=40` → same track shape. IDs are validated, limits clamped to 1–100.

Library calls require verified authentication; absent accounts return 400, provider outages return sanitized 502 and mark account attention. These routes expose library data to Music consumers but the current Music client must add library browsing/import UI before it will display personal playlists. This repository does not modify Music. Limits are bounded previews, not pagination/full-library export; no write/rating API is added.

## Verification and remaining gate

Run `python -m pytest -q` with requirements and pytest installed, and `node --test tests/test_ui.cjs`. Tests use the real installed OAuthCredentials with a mocked HTTP transport plus mocked authenticated library responses. Private pairing tests exercise real local HTTP guards/readback and a real local SOCKS authentication/remote-DNS exchange that refuses forwarding, plus transport-unit tests of TLS endpoint selection, status-line limits and deadline/no-auth refusal. JS tests exercise file selection, authorization, clearing, insecure/malformed rejection and separate explicit probe actions. These are offline fixture proofs, **not** a successful home relay or Google pairing. No owner account was connected and no live relay/deployment was changed. Native Mac/Windows install, private file transfer, scoped Tailscale access, HTTPS/import-key provisioning, Google client/consent and real home relay/library/playback checks remain owner-only gates.
