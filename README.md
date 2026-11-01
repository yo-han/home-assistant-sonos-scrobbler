# Home Assistant Sonos Scrobbler

A Home Assistant custom integration that observes Spotify on the Sonos speakers you
include, each with its own switch. It shows the current track and speaker on local
sensors and has two independent Last.fm switches: Now Playing and scrobbling. Delivery is off by default. Linking an
account alone does not send listening data. If Spotify already scrobbles to your
Last.fm account, leave that connection in place and leave scrobbling off here unless
you accept the overlap described below.

It uses Home Assistant's built-in Sonos integration and sends no speaker commands. No
cloud service or separate container is needed; requests to Last.fm go directly from
Home Assistant, and only when you turn on a switch.

## Requirements

- Home Assistant Core 2026.10.4 or newer. The full test suite runs on 2026.10.4;
  separate runtime checks cover Marketplace installation and migration on 2026.11.0b1.
  See [compatibility verification](docs/marketplace.md) for scope and limits.
- The built-in Sonos integration, set up with at least one enabled speaker.
- Spotify playing on Sonos. Other sources are not sent.
- For sending to Last.fm: a Last.fm account and your own free API application. The
  local sensors need neither.

One installation supports one confirmed Last.fm account and up to 64 included Sonos
players. Above 64 enabled speakers, included speakers come first and the rest fill the
remaining switches by entity id; the others get no switch, which the log and the
`speakers_left_out` status attribute report. Turning a speaker off can then remove its
switch, because it no longer sorts first. To bring it back, select it in **Reconfigure**,
which lists every enabled Sonos speaker; nothing is selected for you. There is no
ListenBrainz support and there are no per-room or per-person accounts.

## Installation

Use the built-in Marketplace on Home Assistant 2026.11 or newer, HACS on
2026.10.4, or the manual package from a release. This is a community integration.
It is not included in the shared Marketplace/HACS catalog yet; add its repository
using the instructions below.

[![Open the repository in Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=yo-han&repository=home-assistant-sonos-scrobbler&category=integration)

The button opens HACS before 2026.11 and Marketplace from 2026.11 onwards.

### Marketplace: Home Assistant 2026.11 or newer

Home Assistant 2026.11 is currently in beta, with its stable release planned for
November 4, 2026. These instructions follow the
[official Marketplace documentation](https://contributors.home-assistant.io/marketplace/how-it-works/).

1. Open **Settings → Marketplace** as an administrator. Marketplace comes with
   Home Assistant; no separate HACS installation is needed.
2. Read and accept the community-project notice before installing anything.
3. Choose **Add from link** and enter
   `https://github.com/yo-han/home-assistant-sonos-scrobbler`.
   Choose **Integration** if asked for the type.
4. Connect GitHub when prompted. Adding a repository outside the shared catalog
   requires a GitHub connection, even though catalog installations do not.
5. Install **Home Assistant Sonos Scrobbler**. On a clean first installation,
   continue into its setup flow without restarting. If Home Assistant already
   loaded this integration, follow the restart repair first.

Updates appear under **Settings → Updates**. Updating an integration that is
already loaded still requires a Home Assistant restart.

### HACS: Home Assistant 2026.10.4

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/yo-han/home-assistant-sonos-scrobbler` with type **Integration**.
3. Install **Home Assistant Sonos Scrobbler** and restart Home Assistant.

HACS offers releases only, not the development branch. It installs just the
`custom_components/personal_sonos_scrobbler/` folder.

Continue with [Setup](#setup) after restarting.

### Moving from HACS to Marketplace

Take a backup before upgrading Home Assistant. From 2026.11, Home Assistant
automatically takes over HACS's installed projects, custom repositories and update
entities. Existing HACS links open Marketplace. Do not install HACS again or remove
and re-add this integration during the transition.

This integration keeps its existing domain, `personal_sonos_scrobbler`, and storage
identities. Keep its configuration entry: it holds the Last.fm account and speaker
choices. An upgrade must preserve those choices and the two delivery switches;
it must not turn scrobbling on. Verify them after the Home Assistant upgrade.
See [compatibility verification](docs/marketplace.md) for the exact versions tested
and the distinction between automated tests and a live installation.

### Manual package

1. Take a Home Assistant backup before upgrading. A config entry from before 0.3.0
   migrates to version 2; returning to 0.2.2 requires the earlier backup.
   Returning from 0.5.0 to 0.4.0 loads with delivery off; restore the backup to keep
   its setting.
2. Download the ZIP and its checksum from the
   [latest release](https://github.com/yo-han/home-assistant-sonos-scrobbler/releases/latest):

   ```sh
   BASE=https://github.com/yo-han/home-assistant-sonos-scrobbler/releases/download/v0.8.1
   curl -fLO "$BASE/personal_sonos_scrobbler-0.8.1.zip"
   curl -fLO "$BASE/personal_sonos_scrobbler-0.8.1.zip.sha256"
   ```

3. Verify with `shasum -a 256 -c personal_sonos_scrobbler-0.8.1.zip.sha256`, which must
   print `OK`.
4. Extract into the Home Assistant configuration directory so files are in
   `custom_components/personal_sonos_scrobbler/`, replacing the previous code.
5. Restart Home Assistant. An existing entry keeps its selected players and linked
   account: those speakers appear with their switch on, every other speaker with its
   switch off. Upgrading from 0.4.0 keeps Now Playing as it was and leaves scrobbling
   off, whatever the old entry stored. A new entry starts with both switches off.
   The default name of the device and entry becomes Home Assistant Sonos Scrobbler.
   Builds before 0.8.0 also lose the speaker switches’ old **Include** or Dutch
   **Meenemen** prefix.
   Entity ids, automations and any name you set yourself stay as they were.

Do not delete the integration as an upgrade step: deletion removes its local data.

## Setup

Go to **Settings → Devices & services → Add integration**, choose
**Home Assistant Sonos Scrobbler** and select the Sonos players to include, or select
none and use the switches afterwards. Nothing is selected for you. There is no YAML. Only enabled
`media_player` entities registered by the built-in `sonos` integration can be selected.

The local sensors work straight away for the speakers you included. Link an account only if you want to send to
Last.fm, then choose what is sent with the two switches on the integration's device.

## Rooms and groups

**Every Sonos speaker has its own switch**, shown with just the speaker's name, in the
**Configuration** section of the integration's device. The name follows the Sonos
entity; a name you give the switch yourself stays. The two Last.fm switches stay under
**Controls**. The list follows Home Assistant: a new speaker appears with its switch
off, a renamed one keeps its switch and its choice, and a removed or disabled one
loses its switch. A disabled speaker that returns has its choice back.

**A returning speaker depends on its identity.** If Home Assistant restores a removed
registry entry with the same unique ID, it is the same speaker and keeps its earlier
choice, on or off. A speaker with a new identity starts off. A choice for a removed
speaker stays stored until Reconfigure replaces the selection.

A speaker's switch controls everything this integration does for that room: the Current
track and Current speaker sensors, Now Playing and scrobbling. A room that is off gives
no local display, no notice and no listening credit here, also with both Last.fm
switches on. The switches work without a linked account and with both Last.fm switches
off, and all of them may be off: the integration stays loaded and the sensors clear.

**These switches do not govern your direct Spotify → Last.fm connection.** That is an
independent sender, which this integration can neither see nor stop. If children or
another person use your Spotify account, Spotify can still scrobble their listening to
your Last.fm account, even while the room's switch here is off.

Turning a room on allows its current Spotify track straight away on the sensors and,
if that switch is on, on Now Playing. It does not backfill history: only a listen that
starts afterwards can be scrobbled. **Reconfigure** does the same as the switches, for
several rooms at once.

**Changing the rooms starts a new scrobbling period.** Queued plays do not record which
room they came from, so with scrobbling on, every change parks all queued plays until
their expiry and drops listens in progress, in the other rooms too. Queued plays are
not deleted, and both Last.fm switches stay as they were. The Scrobbling switch can read
off for a moment during the change. If the local storage fails then, or Home Assistant
stops at that moment, scrobbling stays off until you turn it on again.

All included rooms share one Current track sensor, one Current speaker sensor, one
Last.fm account and the same two switches. There is no set of sensors per room.

- **A group is one listening stream.** Grouped speakers show one track, read from the
  group's coordinator, and do not create one scrobble per speaker. A group counts as
  soon as one of its members is included, even if the coordinator is off, because the
  music is the same; a group without an included member counts for nothing. To keep a
  room out entirely, do not group it with an included room.
- **Independent rooms compete for the one display.** The newest started or resumed
  track shows, and Last.fm's Now Playing announces the newest arrival too. When that
  room stops, the sensors show the room before it again. For scrobbling, independent rooms are separate
  streams, each earning its own listening credit.

## Languages

The interface is available in English and Dutch: setup, Reconfigure, the account and
queue menus, entity names, sensor states and error messages. Forms and states follow the
language of your Home Assistant profile, and entity names follow the system language.
Other languages show English. This documentation is in English.

Speaker names, track metadata and the stable attribute names are not translated. The
**Configuration** and **Controls** headings on the device page belong to Home Assistant,
which translates them itself; this integration cannot rename them. Changing the
language does not change existing entity ids.

## Account linking

Account linking is optional and sends no listening data. Authorization requests
happen when you submit the link flow yourself.

1. Create an application at <https://www.last.fm/api/account/create>. Leave the
   callback URL empty: this uses desktop authorization, so your Home Assistant does not
   have to be reachable from the internet.
2. Choose **Configure → Connect a Last.fm account** on the integration.
3. Enter your API key and shared secret. Your Last.fm password is never needed.
4. Open the Last.fm link, approve access, then return to Home Assistant and submit.
5. Check and confirm the returned account. A pending attempt expires after ten
   minutes; pending values are dropped when it expires or is cancelled.

API requests use a form POST to a fixed HTTPS endpoint. The browser link necessarily
holds the API key and a one-use token. The shared secret and session key are never
in a URL.

The API key, shared secret, session key and account name are saved in Home Assistant's
config entry. They are **not encrypted by this integration**, and are included in
backups. Anyone who can read those files can read the credentials. Disconnecting
removes the local credentials but does not revoke access on Last.fm: revoke it in
your Last.fm account settings. Replacing or unlinking an account turns both switches
off in one write, and relinking turns neither on; old queued data is never redirected
to another account.

`lastfm_linked` means confirmed credentials are stored locally. It does not itself
check that the account still works. Delivery requests can detect an invalid or
suspended API key or session and then require relinking.

## Delivery and overlap

Start with observation only. Once an account is linked, the integration's device has
two ordinary switches, without a confirmation form. Turning one on is your consent to
sending real track data; the other is left alone.

| Now Playing to Last.fm | Scrobbling to Last.fm | What happens |
|---|---|---|
| off | off | No Last.fm request at all; the local sensor keeps working |
| on | off | The current track is announced; nothing enters your history |
| off | on | New listens are scrobbled, nothing is announced |
| on | on | Both, sharing one request pacing, cooldown and authentication stop |

**Scrobbling overlaps with Spotify.** A live test of 0.3.0 showed a late overlapping
write from an existing Spotify connection. That overlap remains possible if you turn
scrobbling on while Spotify also sends: the checks below reduce it but cannot exclude
it, and a duplicate cannot be removed from here. Upgrading never turns scrobbling on.

A switch shows what is saved, not what you clicked. Turning one on returns only after
the ledger committed it, and fails with a message if no account is linked or storage
failed. Turning one off works at once, also after a storage error, and stops the
sender: a request already on the wire cannot be recalled, and a scrobble caught there
stays uncertain. Turning Now Playing off or on costs no listening credit and does not
touch a running scrobble.

**Scrobbling is future-only.** Only a listen that starts after you turn the switch on
can be queued. The track already playing is not backfilled, and off followed by on
starts a new period. Plays queued, recovered or left uncertain before that moment,
including from 0.3.0 and 0.4.0, are parked: they cause no `user.getRecentTracks` or
`track.scrobble` request, stay in the local ledger with their receipts until they
expire or you clear them, and nothing on Last.fm is changed. With scrobbling off, no
such request is made at all, including after a restart. No historical observation
candidates are backfilled.

If the clock went back, the new period starts just after the newest queued play, not at
the earlier clock time, so that play stays parked. A listen that starts before that
moment is not queued; a play stored with a wrong future time delays new listens the
same way, until it expires and you turn the switch off and on.

A track qualifies when duration is strictly above 30 seconds and observed listening
reaches half its duration, at most 240 seconds. Pauses and downtime add no listening
credit. Source evidence must identify Spotify; explicit exclusions take precedence.

Your existing Spotify → Last.fm connection is untouched: this integration cannot see or
change it. To reduce overlap, a queued
play waits until the later of ten minutes after qualification and one minute after
its expected end. Every initial dispatch, including after restart, checks dated
Last.fm history. A matching receipt suppresses the POST; incomplete or unavailable
history holds the play. Requests are paced, and rate limits back off globally.

This does **not** provide exactly-once delivery. An independent late Spotify write
can arrive after the check. Conservative matching and ambiguous playback can also
miss a legitimate listen; this integration prefers that to a duplicate.

If a request might have succeeded but its answer was lost, the item becomes
uncertain. It is only reconciled by history reads and is never automatically
reposted. Explicit provider rejections that permit retry retain the original
timestamp and metadata and get a new preflight check.

Clear queue waits for outstanding work and a committed clear before reporting success,
and surfaces a storage error.

## Now Playing

While the Now Playing switch is on, the integration transmits the current Spotify track
to Last.fm immediately: artist, title and, when known, album and duration. It does not
wait for listening credit. Now Playing has no effect on your history or charts, and a
notice creates no scrobble and no queue row. Only confirmed Spotify on a stable selected group with artist and title is
announced, once per track. Volume, follower and timer updates send nothing; a new
track, a resume after pause, a reload or turning the switch on can send one notice. With
several independent rooms, the newest track start or change is shown; an older room
does not take the display back. Turning the switch on announces the track already
playing; the scrobbling switch does not.

Notices are best effort and kept in memory only. A failed notice is never retried, as
Last.fm requires; one that a cooldown or authentication stop holds back is dropped.
Notices use the stored request pacing and stops of the delivery worker. Last.fm has no
request to clear Now Playing: turning off or stopping prevents a queued notice, but cannot
instantly clear what Last.fm already shows; it expires there by itself. Your Spotify →
Last.fm connection may also update the same display.

## Current track sensor

The **Current track** sensor is local and works with both switches off or without a
linked account. Its state is `artist — title`, cut to Home Assistant's 255-character
state limit. Attributes hold the full `artist` and `title`, `album`, `duration` and the
`speaker`: the group's coordinator entity, selected or not. It has no position,
cover art, content URI or history.

It shows only confirmed, settled Spotify playback with artist and title, and it clears
on pause, stop, a lost or unknown source, an unavailable speaker and missing metadata,
including briefly while a group regroups. One group is one track. With several
independent rooms, the newest started or resumed track shows; a static update of an
older room does not take it back, and when the newest stops the room before it shows
again. Track changes write a state; heartbeats, positions and volume do not. Nothing is
restored after a restart: it stays empty until a track is proven again.

Home Assistant's Recorder stores this sensor's state and attributes under its own
rules, and they end up in backups, so your listening history can be kept there. This
integration does not change your Recorder configuration; exclude the entity there if
you do not want that.

## Current speaker sensor

The **Current speaker** sensor names the room of that same track: its state is the
speaker's Home Assistant name, cut to the same limit, or the entity id when it has no
name, and the `entity_id` attribute holds the Sonos entity. In a group that is the
coordinator, selected or not. It follows the current track exactly, also with both
switches off or without a linked account, and it is empty whenever that sensor is. It
carries no music, and Recorder keeps it under the same rules, so it can show in which
room you listened when.

## Local storage and status

Pending and uncertain items contain the track metadata needed to send or reconcile
them, in a private per-entry SQLite file under the Home Assistant configuration's
`.storage` directory. Credentials are not in this ledger. The active queue is capped
at 1,000 items, with a seven-day deadline. Cleanup runs while the integration is
loaded, including while delivery is paused, and on startup. A storage error stops
cleanup until a reload successfully verifies storage; metadata can remain longer
while Home Assistant is stopped or storage has failed. Expired queued work is not sent. A full queue can lose new listens rather than evict existing work.

Terminal rows have readable track metadata removed. Receipt fingerprints are kept
for up to 14 days and 10,000 rows, subject to active identity protection. Coalesced
identities have a separate bounded alias table. Active/prior/ambiguity markers use
at most three slots per selected speaker and can outlive those ages while a paused
speaker still needs them. These fingerprints are not encrypted.

The two status sensors and integration diagnostics expose fixed states, counts,
durations and flags, not artists, titles, albums, account names, content IDs, raw
timestamps or credentials, and logs carry fixed codes. Only the current track sensor
shows music.
Observation counters are estimates, not proof that music was audible.
The observation sensor is aggregated across selected groups; it does not create one
scrobble per follower. The delivery sensor distinguishes disabled, grace waiting,
retrying, authentication required, uncertain and storage-error states.

After a restart with only Now Playing on, the delivery sensor shows a stored
authentication stop, cooldown or unreadable stop straight away, before another notice is
tried. A stored cooldown clears from the sensor when it ends.

A storage error stops sending and cleanup until a new start. First reload the
integration: this re-verifies the ledger and can recover from a transient lock or
storage failure. A missing or damaged ledger is never silently replaced with an
empty one. Do not delete it merely to dismiss an error: that loses duplicate
protection. If reloading still fails, inspect the fixed error code and restore a
suitable backup if needed.
A large forward clock error can leave delivery waiting when the clock is corrected
backwards; it fails closed rather than guessing that old work is safe to repeat.

Backups can contain both queue metadata and credentials. Restoring an older backup
can lose later local receipts; preflight checks reduce that risk but cannot make a
backup restore exactly-once. The integration does not erase data from older backups.

## Reset behavior

| Event | Observation | Durable delivery data |
|---|---|---|
| HA restart or integration reload | Current credit and observation counters reset; downtime earns nothing | Queue, receipts, saved identities and delivery totals persist; both switches and the scrobbling period stay |
| Speaker selection change | The speakers that stay keep observing; a speaker that left ends at once | Deselected identity markers are purged; with scrobbling on a new period starts, so all queued plays park; both switches stay |
| A switch turned off | Observation continues | Queue remains subject to expiry; no new dispatch of that feature |
| Scrobbling turned on | Observation continues | Earlier rows stay parked until expiry; only new listens count |
| Account unlinked or replaced | Observation continues | Both switches off; work stays bound to its original destination |
| Clear queue | Observation continues | Queued metadata is discarded after a committed clear; dedupe identities remain |
| Integration deleted | Observation stops | Config credentials and the integration's ledger/journal are removed after writers stop |

## Spotify evidence

`source = Spotify Connect` or a `spotify:track:` content ID (including one inside
`x-sonos-spotify:`) confirms Spotify. TV, line-in, AirPlay, radio/media-channel,
network-share and identifiable podcast/video content are excluded. Anything else is
unknown and cannot be sent. Sonos reports `media_content_type = music` broadly, so
that alone is not evidence; a podcast exposed only as Spotify Connect may qualify.

## Limits

- Muted playback and Sonos `TRANSITIONING` mapped to `playing` can count.
- Repeats, seeks and regrouping are not perfectly distinguishable. An ambiguous
  same-track restart or lost group identity may be held until a proven fresh track.
- Long pauses can let the grace period finish before Spotify writes its scrobble;
  its timestamp may also fall outside the history window. Spotify's timestamp
  behavior is unverified, so these can cause overlap despite the preflight check.
- A guard can hold the whole group, including an unguarded selected leader.
  After a proven fresh own-queue track clears it, returning to an earlier track
  mid-way can count again. Switching away and back can suppress a genuine repeat.
- A claim fenced before its POST can become uncertain with nothing sent. Expired,
  rejected and discarded plays add no delivery total; counts do not show every loss.
- Identity uses artist and title; changing only the album does not prove a new play.
- Metadata gaps contribute no credit. Gaps longer than 45 seconds and long or
  negative timer gaps can make a play ambiguous and cost a listen.
- A position that stays at zero can look like repeated restarts. An old position
  timestamp alone is not a freshness timeout: Sonos can leave it unchanged normally.
- Observed time is capped at 86,400 seconds and observation counters at 1,000,000.
  Observation updates during playback run every 15 seconds, about four Recorder
  rows per minute. Home Assistant retains its Recorder history under its own rules.

## Troubleshooting

| What you see | What to check |
|---|---|
| A room cannot be selected | It must be an enabled `media_player` of the built-in Sonos integration |
| A room shows nothing | Is its switch on, is it playing Spotify, not paused? |
| A speaker has no switch | It must be enabled; beyond 64 speakers, see `speakers_left_out` on the Observation status sensor and select it in **Reconfigure** |
| The sensor is empty while music plays | The source must be proven Spotify with artist and title; a regrouping group clears it briefly |
| A speaker has an old name such as `include` in its entity id | Entity ids stay on upgrade so automations keep working; rename it in Home Assistant if you prefer |
| The sensor shows another room | Another included room started a track later; see [Rooms and groups](#rooms-and-groups) |
| Two rows on Last.fm for one listen | Most likely Spotify and this integration both scrobbled; turn scrobbling off here |
| Delivery status says reauthorization is required | Last.fm rejected the key or session; link the account again |
| Delivery status says storage error | Reload the integration, see [Local storage and status](#local-storage-and-status) |

Check **Settings → System → Logs** for the integration's fixed code. Logs carry no
response bodies, provider messages, credentials, account names or track metadata.

## Removal

Delete the integration in **Settings → Devices & services**, remove its folder from
`custom_components`, then restart Home Assistant. Removing files alone does not
remove the saved entry. Older backups and Recorder history retain their data;
Last.fm access remains valid until you revoke it there.

## Development

See [docs/development.md](docs/development.md), [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). Changes per version are in [CHANGELOG.md](CHANGELOG.md).
This project is released under the [MIT license](LICENSE).
