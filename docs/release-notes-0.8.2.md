# Home Assistant Sonos Scrobbler 0.8.2

Prepare for the built-in Marketplace in Home Assistant 2026.11 while keeping
Home Assistant **2026.10.4** as the minimum supported version.

- Document Marketplace installation, the existing HACS path and automatic HACS
  handover. Include an installation link that works with both stores.
- Classify the integration under music and radio services for Marketplace discovery.
- Verify clean installation and persisted HACS migration with real Home Assistant
  2026.10.4 and 2026.11.0b1 runtimes in isolated containers. Check account settings,
  included/excluded rooms, delivery switches, custom names, registry identities
  and the retained SQLite sent receipt and counter.
- Run the same check against a published source archive, and preserve host file
  ownership between test phases on Linux.
- Stabilize asynchronous delivery tests without changing delivery behavior.

Existing accounts, speaker choices, entity IDs and delivery settings are preserved.
New installations still start with both Last.fm senders off. This update does not
turn scrobbling on or remove the possibility of overlap with a separate Spotify
scrobbler when both senders are enabled. The component domain stays
`personal_sonos_scrobbler`; do not delete or re-add the integration to update it.

The complete test suite runs on 2026.10.4. The isolated Marketplace checks cover
**2026.11.0b1**, not the future stable 2026.11 release. They use synthetic speaker,
GitHub authorization and HACS storage fixtures. Browser authorization and live
Last.fm delivery are outside their scope; the tests send no Last.fm requests.
See [verification scope](https://github.com/yo-han/home-assistant-sonos-scrobbler/blob/v0.8.2/docs/marketplace.md).

Install or update using the repository link in the README. Catalog inclusion is
subject to HACS/Marketplace review; a release alone does not add it to the catalog.
For manual installation, download `personal_sonos_scrobbler-0.8.2.zip` and its
`.sha256` file, verify the checksum, extract into your Home Assistant configuration
directory and restart Home Assistant. Take a backup first and keep the existing
configuration entry. Updating this already loaded integration requires a restart;
Home Assistant itself does not need to be upgraded.
