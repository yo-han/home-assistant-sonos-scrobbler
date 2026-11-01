# Development

Engineering notes for contributors. User documentation is in the
[README](../README.md).

## Layout

- `custom_components/personal_sonos_scrobbler/`: the integration, and all HACS installs.
- `custom_components/personal_sonos_scrobbler/brand/`: the icons Home Assistant shows.
- `assets/brand/icon.svg`: their editable source, not shipped.
- `speaker_controls.py` in the integration: the speaker catalog and the one writer of
  the selection. Every change, from a switch or Reconfigure, goes through it and
  `FeatureControl.async_change_selection`; the runtime rebinds without a reload.
- `scripts/build_package.py`: the deterministic manual ZIP with a fixed allowlist.
- `postman/`: a local synthetic Last.fm mock and simulations. Each simulation's
  `.context.yaml` records its assumptions, provenance and limits.
- `hacs.json` and `.github/workflows/validate.yml`: HACS and hassfest validation.

`strings.json` must equal `translations/en.json`. HACS copies it along, but the manual
ZIP leaves it out, as Home Assistant only reads `translations/` at runtime.

## Checks

Use Python 3.14 and uv. `make test` runs lockfile, Ruff, formatting, strict mypy,
pytest and the deterministic package build/check. Tests use real Home Assistant and
SoCo code with mocked speakers, real SQLite files and synthetic HTTP servers.

`make simulate` and `make simulate-delivery` use a local synthetic Postman mock and
the real clients/worker/storage. They require the Postman CLI and are separate from
`make test` and CI. No login, upload or real provider call is involved. A passing
simulation proves the exercised stand-in scenarios, not the real provider's behavior,
and it runs the worker directly, so it does not prove that the switches gate anything.

The Validate workflow runs on ready pull requests, pushes to `main` and manual dispatch,
without a schedule. Hassfest can run locally; on arm64 it needs emulation:

```sh
docker run --rm --platform linux/amd64 -v "$PWD:/github/workspace" \
  ghcr.io/home-assistant/hassfest
```

## Brand icons

The icons are original work. Do not use third-party logos or Home Assistant branding.
After changing the SVG, render it transparently to 256x256 `icon.png` and 512x512
`icon@2x.png`, for example with resvg, and reduce both to an indexed PNG that keeps its
alpha channel. `tests/test_distribution.py` checks size and transparency.

## Last.fm notes

Request tokens are opaque values, not restricted to hexadecimal: a live response already
demonstrated that the documentation's older hexadecimal example is not a validator.
The JSON response shape, history pagination and POST reads are covered by synthetic
contract tests; Now Playing still needs a live check.

Unexpected provider responses and storage failures use fixed rejection/error codes.
They do not log response bodies, provider messages, credentials, account names or
track metadata.

## Live verification so far

A live check used one selected Sonos player with Spotify Connect. Source recognition,
advancing credit, threshold candidates, track changes and redacted diagnostics were
checked. A real account also completed Last.fm linking on 0.2.2.
Other devices and playback scenarios have not been validated live. A 0.3.0 test sent
one accepted scrobble, and the public profile later showed two rows for one confirmed
listen. Last.fm does not show which sender wrote which row, so that attribution is
inferred; this overlap is why 0.4.0 made Now Playing the only sender and why scrobbling
stays a manual choice.

The switches and sensors since 0.5.0 are tested locally only, on real Home Assistant
services with SQLite and a loopback server; turning scrobbling on against the real
Last.fm API has not been tested. A live check of Now Playing is the immediate status on
the profile, with historical send counters unchanged. No history read is needed with
scrobbling off. The speaker switches of 0.7.0 and the names and languages of 0.8.0 are
tested the same way and not live.
