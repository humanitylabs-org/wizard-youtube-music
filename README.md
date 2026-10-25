# YouTube Music for Wizard

An unofficial music source for Wizard's Music app. Contract `wizard.music-source/1` remains compatible for search, radio and audio; optional account-library endpoints are additive. MIT licensed.

## Playback and home relay

At home, test playback directly. Cloud/VPS addresses may be blocked by YouTube; use [Wizard Home Relay](https://github.com/humanitylabs-org/wizard-home-relay) on an always-on home Mac, Windows or Linux computer if needed. Follow that project's instructions for your platform; this connector does not install or manage a relay. Keep both devices on your private Tailscale network, restrict access to the authenticated SOCKS service and never expose it publicly. Enter its secret address only in this app's private relay form. A relay must remain awake and online. Existing relay settings are preserved by account setup.

**No guarantee of ad-free, unlimited or uninterrupted playback, Premium entitlements, or account safety.** A residential connection does not guarantee playback or reduced account risk. Respect YouTube's terms, rights and account entitlements. Catalog account authorization does not authenticate yt-dlp playback; no account tokens are handed to the downloader.

The setup screen includes **Help me set this up**: a copyable, non-secret request and the shell's supported `wizard:open-app` / `agents` navigation. The owner chooses a chat and sends the request. There is no claimed automatic message delivery, notification or reply subscription. Standalone/older shells fall back to manual navigation. No agent identity is guessed.

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

Run `python -m pytest -q` with requirements installed, and `node --test tests/test_ui.cjs`. Tests use the real installed OAuthCredentials with a mocked HTTP transport plus mocked authenticated library responses. They exercise HTTP routing, CSRF rejection, private storage, connection states, disconnect, provider refusal/expiry/slow-down, relay isolation and browser actions. No owner account was connected and no live relay/deployment was changed. Owner-provisioned OAuth credentials, Google consent and a real library request are the remaining integration gate; playback needs its own real test.
