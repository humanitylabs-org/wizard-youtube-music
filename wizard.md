---
wizard: 0.2
id: wizard-youtube-music
name: YouTube Music
type: service
category: system
version: 0.1.0
summary: Plays YouTube Music in your Music app. Needs a home internet connection; cloud Wizards need a home relay.
tags: [audio]
license: MIT
source: https://github.com/humanitylabs-org/wizard-youtube-music
icon: icon.png
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 9206
  path: /api
mcp:
  path: /mcp
  port: 9206
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 9206
health: /healthz
provides: [music-source, youtube-music]
uses: []
needs: [home-network]
---

# YouTube Music

A music source for Wizard's Music app, like SimpleFIN is a bank source for Money. Music finds it automatically and shows YouTube search, radio and songs.

**Needs a home internet connection.** YouTube blocks cloud servers in data centers. It works on a Wizard at home. On a cloud Wizard (VPS), add a home relay: run Wizard Home Relay on an always-on computer at home that's on your Tailscale and paste its address into this app. The setup screen runs a real test download before it says it's working.

Contract `wizard.music-source/1`: `GET /api/status`, `/api/music/search?q=`, `/api/music/radio?seed=`, `/api/music/related-artists?name=`, `/api/music/genre?genre=`, `/api/music/audio/<videoId>` (Range supported). Errors return 502 with `error: blocked` when YouTube refuses this network.
