---
name: wizard-youtube-music
description: Use when YouTube Music won't play in Music, or to check or set up the YouTube Music connector.
---
The YouTube Music connector feeds the Music app (contract wizard.music-source/1). Call youtube_music_status first. `ready: false` with playback.state `blocked` means YouTube is refusing this Wizard's network: true on cloud servers. The fixes are running Wizard at home, or a home relay (humanitylabs-org/wizard-home-relay on a home computer on Tailscale) pasted into the connector's screen. youtube_music_test runs a real download; never call playback working without it. A bot check here has no puzzle to solve, so don't ask the owner to solve one. Never use another person's cookies or account.
