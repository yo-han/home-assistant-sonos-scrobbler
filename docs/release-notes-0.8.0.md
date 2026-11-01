# Personal Sonos Scrobbler 0.8.0

The first public version of a Home Assistant custom integration that observes Spotify
on the Sonos speakers you include. It shows the current track and speaker on local
sensors and can send to Last.fm with two independent switches: Now Playing and
scrobbling. Earlier versions were private development builds.

## What it does

- Every Sonos speaker gets its own switch, shown with just the speaker's name. A speaker
  that is off takes part in nothing here: no local display, no notice, no listening
  credit.
- Local **Current track** and **Current speaker** sensors work without a Last.fm account.
- **Now Playing to Last.fm** announces the current track and adds nothing to your
  history.
- **Scrobbling to Last.fm** sends only listens that start after you turn it on, through
  a durable local queue with a preflight check against your Last.fm history.
- Grouped speakers count as one listening stream. Independent rooms share one display:
  the newest arrival shows.
- The interface is available in English and Dutch.

## Defaults

Nothing is selected for you and nothing is sent by default. New speakers start off and
both Last.fm switches start off. Linking an account sends no listening data. The
integration reads Sonos and never sends speaker commands.

## Requirements

- Home Assistant Core 2026.10.4 or newer. Tests run on 2026.10.4.
- The built-in Sonos integration with at least one enabled speaker.
- Spotify playing on Sonos. Other sources are not sent.
- For sending to Last.fm: a Last.fm account and your own free API application.

## Installation

**HACS custom repository:** in HACS choose **Custom repositories**, add
`https://github.com/yo-han/home-assistant-sonos-scrobbler` with type **Integration**,
install **Personal Sonos Scrobbler** and restart Home Assistant.

**Manual package:** download `personal_sonos_scrobbler-0.8.0.zip` and its `.sha256` file
from this release, verify with
`shasum -a 256 -c personal_sonos_scrobbler-0.8.0.zip.sha256`, extract into your Home
Assistant configuration directory and restart.

The integration is not in the HACS default catalog.

## Limits to know

- **Scrobbling can overlap with Spotify.** Spotify's own Last.fm connection keeps
  sending and cannot be seen or stopped from here. The preflight check reduces
  duplicates but cannot exclude them, and a duplicate cannot be removed from here. If
  Spotify already scrobbles for you, leave scrobbling off here.
- This is not exactly-once delivery. Ambiguous playback can cost a legitimate listen,
  which the integration prefers to a duplicate.
- The speaker switches do not govern your direct Spotify → Last.fm connection.
- One installation supports one Last.fm account and 64 included speakers. Above 64
  enabled speakers some have no switch; `speakers_left_out` reports how many, and
  Reconfigure can select any enabled speaker.
- A removed speaker that Home Assistant restores under the same identity keeps its
  earlier choice; a speaker with a new identity starts off.
- The **Configuration** and **Controls** headings belong to Home Assistant.
- Credentials are stored unencrypted in Home Assistant's config entry and in backups.
  The local queue holds track metadata, and Recorder stores what the Current track and
  Current speaker sensors show.
- Scrobbling against the real Last.fm API has not been tested for this version.

See the [README](https://github.com/yo-han/home-assistant-sonos-scrobbler#readme) for
the full behavior, privacy notes and troubleshooting.
