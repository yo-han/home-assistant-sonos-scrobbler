"""Constants of the integration."""

DOMAIN = "personal_sonos_scrobbler"
CONFIG_VERSION = 2
CONFIG_MINOR_VERSION = 3
FEATURES_MINOR_VERSION = 2  # the minor version that split the delivery mode
CONF_ENTITY_REGISTRY_ID = "entity_registry_id"  # version 1 only; migrated away
CONF_ENTITY_REGISTRY_IDS = "entity_registry_ids"
MAX_SELECTED_PLAYERS = 64
# Read once by the migration to minor version 2, then gone.
CONF_DELIVERY_ENABLED = "delivery_enabled"
CONF_DELIVERY_ENABLED_AT = "delivery_enabled_at"
CONF_HISTORICAL_DELIVERY = "historical_delivery"
# Only a stored True sends; anything else leaves history to Spotify.
CONF_NOW_PLAYING = "now_playing_enabled"
CONF_NOW_PLAYING_EPOCH = "now_playing_epoch"
CONF_SCROBBLING = "scrobbling_enabled"
CONF_SCROBBLING_SINCE = "scrobbling_since"  # UTC seconds, None while off
# The epoch of the ledger and its scrobbling worker.
CONF_DELIVERY_EPOCH = "delivery_epoch"
CONF_OUTBOX_CREATED = "outbox_created"
CONF_SHARED_SECRET = "shared_secret"
CONF_SESSION_KEY = "session_key"
CONF_ACCOUNT_NAME = "account_name"
CONF_LASTFM = "lastfm"
CONF_LASTFM_LINK_REVISION = "lastfm_link_revision"
MODE_OBSERVATION_ONLY = "observation_only"
MODE_DELIVERY_ENABLED = "delivery_enabled"
ESTIMATE_BASIS = "ha_reported_playback"
TITLE = "Personal Sonos Scrobbler"
LEGACY_TITLE = "Sonos observation pilot"  # the default up to 0.7.0
SIGNAL_LASTFM_LINK_CHANGED = f"{DOMAIN}_lastfm_link_changed_{{}}"
