# Changelog

Versions follow the integration's `manifest.json`.

## Unreleased

- Document Marketplace installation on Home Assistant 2026.11 and migration from
  HACS, while retaining installation on the supported 2026.10.4 minimum.
- Categorize the integration under music and radio services for Marketplace.
- Keep the component domain, stored account, speaker choices and delivery flags
  unchanged during the store transition.

## 0.8.1 (2026-11-01)

- Use Home Assistant Sonos Scrobbler in Home Assistant, HACS, documentation and the
  editable brand asset.
- Update the earlier default entry and device names on upgrade. Keep custom names,
  entity IDs, the Last.fm account, speaker selections and both delivery switches.
- Keep the integration domain and storage identities unchanged for existing installs.

## 0.8.0 (2026-11-01)

The first public version.

- Name each speaker switch after the speaker alone, without a prefix. Entity ids and
  names you set yourself stay as they were.
- Name new installations consistently and replace the old pilot name on
  upgrade; a title you chose stays.
- Complete the English and Dutch interface.
- Add no switch to a platform that is unloading or already unloaded when the speaker
  list changes during a slow stop.
- Let Reconfigure clear the selection however the form submits it: a cleared field no
  longer brings the old selection back.
- Show a stored authentication stop, cooldown or unreadable stop after a restart with
  only Now Playing on, instead of ready.
- Keep track of a Now Playing sender that starts while a finished one is cleaned up.
- Require every delivery fence to state its period explicitly.

## Earlier versions

Everything below was a private development build and was never released.

## 0.7.0

- Add an inclusion switch for every Sonos speaker, in the device's Configuration
  section, found dynamically from the entity registry. A speaker that is off takes
  part in nothing: local sensors, Now Playing and scrobbling.
- New speakers start off; speakers selected before keep their place and start on.
- Allow an entry with no speaker selected, and make Reconfigure work like the switches:
  it applies at once, without a reload.
- Start a new scrobbling period on every change of the selection, so queued plays park.

## 0.6.0

- Add the local **Current speaker** sensor, which names the room of the current track.
- Add HACS metadata, original brand icons and HACS and hassfest validation.
- Release under the MIT license and rewrite the documentation for ordinary users.

## 0.5.0

- Replace the delivery mode with independent Now Playing and scrobbling switches.
- Add the local **Current track** sensor.
- Make scrobbling future-only: older queued plays stay parked.

## 0.4.0

- Make Now Playing the only sender, as a live test showed overlap with Spotify.

## 0.3.0

- Add durable Last.fm delivery with a local ledger and preflight history checks.

## 0.2.x

- Add Last.fm account linking through desktop authorization.

## 0.1.x

- Add read-only observation of Spotify on selected Sonos speakers.
