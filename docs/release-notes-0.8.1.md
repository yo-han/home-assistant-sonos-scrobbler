# Home Assistant Sonos Scrobbler 0.8.1

The integration now uses **Home Assistant Sonos Scrobbler** in Home Assistant,
HACS and its documentation.

Existing installations adopt the new default entry and device name after updating
and restarting Home Assistant. Custom names, entity IDs, automations, the linked
Last.fm account, speaker choices and both Last.fm switches are preserved.

The integration domain remains `personal_sonos_scrobbler`, so no removal, new setup
or account relinking is needed. This update does not enable either sender.

Install or update through the HACS custom repository with type **Integration**.
For manual installation, download `personal_sonos_scrobbler-0.8.1.zip` and its
`.sha256` file, check the checksum, extract into the Home Assistant configuration
directory and restart Home Assistant. Do not delete the existing integration.
