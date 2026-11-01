# Marketplace compatibility

Home Assistant 2026.11 brings HACS into core as Marketplace. Our integration stays
in its own repository and keeps the `personal_sonos_scrobbler` domain. Installation
instructions for users are in the [README](../README.md#installation).

## Supported and tested versions

- The minimum supported Home Assistant remains **2026.10.4**. Its full pytest suite,
  types, lint and package checks remain the baseline gate.
- Marketplace runtime checks use **2026.11.0b1**, the available beta on November 1,
  2026. This is a test of that exact beta, not a claim about the future stable
  release or subsequent betas.
- The test runner pins both official Home Assistant container images by digest and
  verifies the SHA-256 of the HACS **2.0.5** distribution used in the migration fixture.

## Reproduce the installation and migration checks

Use Python 3.10 or newer on the host and Docker with enough room for the two Home
Assistant images. The containers include their own Python and runtime dependencies;
there is no need to install the integration's development dependencies on the host.

Run from the repository root, choosing an output directory that does not exist:

```sh
python3 tests/marketplace/run.py --output /tmp/sonos-marketplace-check
```

The host downloads public GitHub metadata and the checksum-verified HACS archive.
In GitHub Actions, a read-only `GH_TOKEN` avoids the anonymous API rate limit. The
candidate archive is built from this checkout, not downloaded from an unpublished
release. All credentials and listening data are synthetic.

Before publication, CI uses metadata from the pinned `v0.8.1` fixture ref and
installs the current checkout's candidate archive. This avoids depending on a tag
that does not exist yet. After publishing, verify the real public source archive:

```sh
python3 tests/marketplace/run.py --published-release v0.8.2 \
  --output /tmp/sonos-marketplace-published-0.8.2
```

This mode downloads the public tag's actual source ZIP anonymously and installs it
through the same Marketplace backend. Installed component hashes must match this
checkout. The report records the ref and whether the archive was published.

Home Assistant runs on a separate **internal Docker network**, with no published
ports, no production mounts, no Docker socket and a read-only root filesystem. Its
only writable data is the disposable configuration directory and temporary files.
Containers run with the host user's UID and GID, so Home Assistant's private storage
keeps the same owner between phases on native Linux as well as Docker Desktop.
Recorded GitHub responses and the candidate archive are replayed over loopback
HTTP. The runner removes its own containers and network, including on a test failure.
It retains the configuration directories and reports for inspection.

The three phases exercise actual Home Assistant startup:

1. **Clean 2026.11 installation.** Bootstrap the built-in Marketplace, register the
   custom repository through its backend, install the candidate and verify every
   installed component file against its hash. Check that the first install does
   not request a restart, and complete the real integration config flow. Both
   delivery switches start off. Track and speaker sensors follow an included
   synthetic Sonos room and exclude a second room.
2. **2026.10.4 baseline.** Configure the same candidate with an included and an
   excluded synthetic Sonos room. Store a synthetic Last.fm account and enable Now
   Playing using the real switch service, keeping scrobbling off. Create a synthetic
   sent receipt through the ledger API, without sending it anywhere. Preserve a
   user-defined entry name and an entity name, plus registry and device IDs. After
   a clean shutdown, add HACS config/registry and version-6 storage fixtures, with
   the real HACS 2.0.5 files on disk.
3. **2026.11 migration.** Boot the same persisted directory. Check that core hands
   the HACS entry to Marketplace, adopts the installed custom repository and update
   entity, and keeps their IDs. Compare the scrobbler's full config data and registry
   identities, including the synthetic account, room choices and delivery flags.
   Verify SQLite integrity, the retained sent receipt and sent total, then exercise
   the local sensors again. Assert that Marketplace remains enabled and no phase
   makes a Last.fm HTTP request.

The final `verification.json` records outcomes and provenance; per-phase reports,
request logs and Home Assistant logs are stored beside it. CI runs the same command.

## Scope of the evidence

These checks run the real Home Assistant runtimes, installer, configuration flow,
entity platforms and migration code. Speaker states, GitHub authorization and HACS
storage are test fixtures. They do **not** test GitHub's browser/device authorization,
manual clicks in the Marketplace frontend, catalog inclusion, real speaker discovery
or delivery to Last.fm. There is no copy of the owner's Home Assistant configuration.

The stable pytest fixture package currently imports a logger helper removed by
2026.11 and imports HTTP tooling before the beta's required initialization. For
that reason this runtime check is independent of that plugin. The full existing
suite runs on 2026.10.4; it is not skipped or patched to appear green on the beta.
Re-run the runtime check against the final 2026.11 release before claiming stable
2026.11 compatibility.

## Catalog submission and installation links

Both HACS and Marketplace read the catalog maintained in
[`hacs/default`](https://github.com/hacs/default). A public release is already
available, but this repository is not yet listed in that catalog. A catalog
submission remains a separate pull request after validation; the move to
Marketplace does not automatically add this project.

Keep `hacs.json`, the component layout, HACS validation and hassfest. The manifest
uses the `media_entertainment.music_radio_services` category for discovery. Do not
rename the component domain or delete/recreate a config entry to change stores.

The README uses `hacs_repository` because that My Home Assistant link opens HACS on
older Home Assistant versions and Marketplace on 2026.11+. `marketplace_repository`
is available specifically for 2026.11+.

Official references, checked November 1, 2026:

- [Marketplace architecture and migration](https://contributors.home-assistant.io/marketplace/how-it-works/)
- [Publishing custom integrations](https://contributors.home-assistant.io/marketplace/guides/integrations/)
- [Catalog submission and compatible links](https://contributors.home-assistant.io/marketplace/guides/catalog/)
- [Home Assistant 2026.11 beta release notes](https://rc.home-assistant.io/blog/2026/10/28/release-202611/)
