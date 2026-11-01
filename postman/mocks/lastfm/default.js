/**
 * Synthetic stand-in for the Last.fm methods the integration calls.
 *
 * Every value here is invented. The shapes follow the public documentation at
 * last.fm/api, except where lastfm-auth.context.yaml and
 * lastfm-delivery.context.yaml record an assumption, so this mock can show how the
 * client behaves but never that the real API agrees.
 *
 * Members cannot read x-mock-scenario in a simulation, so the API key picks the
 * behavior. The handler checks the POST form and the MD5 signature itself. Each
 * delivery key keeps its own in-memory ledger; simulation.ledger reports it, and no
 * real provider has that method.
 */
const crypto = require("crypto");
const http = require("http");

const PORT = process.env.PORT || 4510;
const SHARED_SECRET = "synthetic-simulation-secret";
// Synthetic 32-character non-hex token: exercise the observed compatibility gap.
const TOKEN = "ghijklmnopqrstuvwxyz0123456789AB";
const SESSION = { name: "synthetic_listener", key: "synthetic-session-key", subscriber: 0 };
const OVERSIZED_BYTES = 70 * 1024;
const OVERSIZED_CHUNK_BYTES = 8 * 1024;
const JSON_HEADER = { "Content-Type": "application/json" };

const KEYS = {
  SUCCESS: "1".repeat(32),
  UNAPPROVED_ONCE: "2".repeat(32),
  API_RATE_LIMITED: "3".repeat(32),
  MALFORMED: "4".repeat(32),
  OVERSIZED: "5".repeat(32),
};
const UNSIGNED = new Set(["format", "callback", "api_sig"]);
const sessionAttempts = new Map();

const deliveryKey = (suffix) => suffix.padStart(32, "d");
const DELIVERY_KEYS = {
  EXISTING_HISTORY: deliveryKey("02"),
  RETRY_11: deliveryKey("03"),
  RETRY_16: deliveryKey("04"),
  RATE_LIMIT: deliveryKey("05"),
  IGNORED: deliveryKey("06"),
  INVALID_SESSION: deliveryKey("07"),
  MALFORMED: deliveryKey("08"),
  OVERSIZED: deliveryKey("09"),
  HISTORY_FAILURE: deliveryKey("0a"),
  INCOMPLETE_PAGE: deliveryKey("0b"),
  RESPONSE_LOST: deliveryKey("0c"),
  RESTART_IN_FLIGHT: deliveryKey("0d"),
  TIMEOUT_AFTER_WRITE: deliveryKey("0f"),
  NOTICE_DELAYED: deliveryKey("10"),
  NOTICE_IGNORED: deliveryKey("11"),
  NOTICE_MALFORMED: deliveryKey("12"),
};
// The last two characters of these keys are the decimal API code to answer with.
const WRITE_ERROR_PREFIX = "a".repeat(30);
const READ_ERROR_PREFIX = "b".repeat(30);
const NOTICE_ERROR_PREFIX = "c".repeat(30);
const FIRST_POST_ERRORS = new Map([
  [DELIVERY_KEYS.RETRY_11, 11],
  [DELIVERY_KEYS.RETRY_16, 16],
  [DELIVERY_KEYS.RATE_LIMIT, 29],
]);
const DELAYED_KEYS = new Set([
  DELIVERY_KEYS.RESTART_IN_FLIGHT,
  DELIVERY_KEYS.TIMEOUT_AFTER_WRITE,
  DELIVERY_KEYS.NOTICE_DELAYED,
]);
// Longer than the deadline the runner sets, shorter than what it waits at the end.
const WRITE_DELAY_MS = 2000;
const HISTORY_PAGES_OF_INCOMPLETE = 2;
const SYNTHETIC_ARTIST = "Synthetic Artist";
const SYNTHETIC_TITLE = "Synthetic Title";
const EXISTING_ROW_OFFSET_SECONDS = 40;
const ledgers = new Map();

function signature(form) {
  const names = [...form.keys()].filter((name) => !UNSIGNED.has(name)).sort();
  const text = names.map((name) => `${name}${form.get(name)}`).join("");
  return crypto.createHash("md5").update(text + SHARED_SECRET, "utf8").digest("hex");
}

function sendJson(res, status, payload) {
  res.writeHead(status, JSON_HEADER);
  res.end(JSON.stringify(payload));
}

function sendApiError(res, error, message) {
  sendJson(res, 200, { error, message });
}

// Valid JSON on purpose: only the client's byte limit can refuse this reply.
function sendOversized(res, payload = { token: TOKEN }) {
  res.writeHead(200, JSON_HEADER);
  res.write(JSON.stringify(payload));
  const chunk = " ".repeat(OVERSIZED_CHUNK_BYTES);
  for (let sent = 0; sent < OVERSIZED_BYTES; sent += OVERSIZED_CHUNK_BYTES) {
    res.write(chunk);
  }
  res.end();
}

function respond(res, form) {
  const apiKey = form.get("api_key");
  if (apiKey === KEYS.API_RATE_LIMITED) {
    return sendApiError(res, 29, "Rate limit exceeded");
  }
  if (apiKey === KEYS.MALFORMED) {
    res.writeHead(200, JSON_HEADER);
    return res.end("{ not json");
  }
  if (apiKey === KEYS.OVERSIZED) {
    return sendOversized(res);
  }
  if (form.get("method") === "auth.getToken") {
    return sendJson(res, 200, { token: TOKEN });
  }
  if (form.get("token") !== TOKEN) {
    return sendApiError(res, 4, "Invalid authentication token");
  }
  const attempt = (sessionAttempts.get(apiKey) || 0) + 1;
  sessionAttempts.set(apiKey, attempt);
  if (apiKey === KEYS.UNAPPROVED_ONCE && attempt === 1) {
    return sendApiError(res, 14, "This token has not been authorized");
  }
  return sendJson(res, 200, { session: SESSION });
}

function ledgerOf(apiKey) {
  if (!ledgers.has(apiKey)) {
    ledgers.set(apiKey, { posts: 0, reads: 0, notices: 0, scrobbles: [] });
  }
  return ledgers.get(apiKey);
}

function acceptedReply(timestamp) {
  return {
    scrobbles: {
      "@attr": { accepted: 1, ignored: 0 },
      scrobble: { timestamp, ignoredMessage: { code: "0", "#text": "" } },
    },
  };
}

function ignoredReply(timestamp) {
  return {
    scrobbles: {
      "@attr": { accepted: 0, ignored: 1 },
      scrobble: {
        timestamp,
        ignoredMessage: { code: "1", "#text": "Artist name failed filter" },
      },
    },
  };
}

function noticeReply(code) {
  return {
    nowplaying: {
      artist: { corrected: "0", "#text": SYNTHETIC_ARTIST },
      track: { corrected: "0", "#text": SYNTHETIC_TITLE },
      album: { corrected: "0", "#text": "" },
      albumArtist: { corrected: "0", "#text": "" },
      ignoredMessage: { code, "#text": "" },
    },
  };
}

function apiCodeOf(apiKey, prefix) {
  return apiKey.startsWith(prefix) ? Number(apiKey.slice(prefix.length)) : null;
}

function respondScrobble(res, form) {
  const apiKey = form.get("api_key");
  const ledger = ledgerOf(apiKey);
  const timestamp = form.get("timestamp");
  ledger.posts += 1;
  if (form.get("sk") !== SESSION.key) {
    return sendApiError(res, 9, "Invalid session key");
  }
  const writeError = apiCodeOf(apiKey, WRITE_ERROR_PREFIX);
  if (writeError !== null) {
    return sendApiError(res, writeError, "Synthetic API error");
  }
  if (apiKey === DELIVERY_KEYS.INVALID_SESSION) {
    return sendApiError(res, 9, "Invalid session key");
  }
  if (FIRST_POST_ERRORS.has(apiKey) && ledger.posts === 1) {
    return sendApiError(res, FIRST_POST_ERRORS.get(apiKey), "Try again later");
  }
  if (apiKey === DELIVERY_KEYS.IGNORED) {
    return sendJson(res, 200, ignoredReply(timestamp));
  }
  if (apiKey === DELIVERY_KEYS.MALFORMED) {
    res.writeHead(200, JSON_HEADER);
    return res.end("{ not json");
  }
  if (apiKey === DELIVERY_KEYS.OVERSIZED) {
    return sendOversized(res, acceptedReply(timestamp));
  }
  // Every path below keeps the play first: what follows can only lose the answer.
  ledger.scrobbles.push({
    artist: form.get("artist"),
    title: form.get("track"),
    timestamp: Number(timestamp),
  });
  if (apiKey === DELIVERY_KEYS.RESPONSE_LOST) {
    return res.socket.destroy();
  }
  if (DELAYED_KEYS.has(apiKey)) {
    setTimeout(() => sendJson(res, 200, acceptedReply(timestamp)), WRITE_DELAY_MS);
    return undefined;
  }
  return sendJson(res, 200, acceptedReply(timestamp));
}

// Never touches ledger.scrobbles: a notice has no history effect.
function respondNowPlaying(res, form) {
  const apiKey = form.get("api_key");
  ledgerOf(apiKey).notices += 1;
  if (form.get("sk") !== SESSION.key) {
    return sendApiError(res, 9, "Invalid session key");
  }
  if (!form.get("artist") || !form.get("track") || form.has("timestamp")) {
    return sendApiError(res, 6, "Invalid parameters");
  }
  const noticeError = apiCodeOf(apiKey, NOTICE_ERROR_PREFIX);
  if (noticeError !== null) {
    return sendApiError(res, noticeError, "Synthetic API error");
  }
  if (apiKey === DELIVERY_KEYS.NOTICE_IGNORED) {
    return sendJson(res, 200, noticeReply("1"));
  }
  if (apiKey === DELIVERY_KEYS.NOTICE_MALFORMED) {
    res.writeHead(200, JSON_HEADER);
    return res.end("{ not json");
  }
  if (DELAYED_KEYS.has(apiKey)) {
    setTimeout(() => sendJson(res, 200, noticeReply("0")), WRITE_DELAY_MS);
    return undefined;
  }
  return sendJson(res, 200, noticeReply("0"));
}

function historyRow({ artist, title, timestamp }) {
  return {
    artist: { mbid: "", "#text": artist },
    name: title,
    date: { uts: String(timestamp), "#text": "synthetic date" },
  };
}

function storedRows(apiKey, form) {
  const after = Number(form.get("from"));
  const before = Number(form.get("to"));
  const rows = [...ledgerOf(apiKey).scrobbles];
  if (apiKey === DELIVERY_KEYS.EXISTING_HISTORY) {
    rows.push({
      artist: SYNTHETIC_ARTIST,
      title: SYNTHETIC_TITLE,
      timestamp: after + EXISTING_ROW_OFFSET_SECONDS,
    });
  }
  // "from" and "to" are documented as exclusive; see the context file.
  return rows
    .filter((row) => row.timestamp > after && row.timestamp < before)
    .map(historyRow);
}

function historyPage(form, rows, totalPages) {
  const page = form.get("page");
  return {
    recenttracks: {
      // A single row is an object: the XML-to-JSON mapping the client must survive.
      track: rows.length === 1 ? rows[0] : rows,
      "@attr": {
        user: form.get("user"),
        page,
        perPage: form.get("limit"),
        totalPages: String(totalPages),
        total: String(rows.length),
      },
    },
  };
}

function respondHistory(res, form) {
  const apiKey = form.get("api_key");
  ledgerOf(apiKey).reads += 1;
  const readError = apiCodeOf(apiKey, READ_ERROR_PREFIX);
  if (readError !== null) {
    return sendApiError(res, readError, "Synthetic API error");
  }
  if (apiKey === DELIVERY_KEYS.HISTORY_FAILURE) {
    return sendJson(res, 500, { message: "Synthetic history failure" });
  }
  if (apiKey === DELIVERY_KEYS.INCOMPLETE_PAGE) {
    const row = historyRow({
      artist: SYNTHETIC_ARTIST,
      title: "Another Synthetic Title",
      timestamp: Number(form.get("from")) + EXISTING_ROW_OFFSET_SECONDS,
    });
    // The second page announced by the first arrives with an undated row.
    const rows = form.get("page") === "1" ? [row] : [{ ...row, date: undefined }];
    return sendJson(res, 200, historyPage(form, rows, HISTORY_PAGES_OF_INCOMPLETE));
  }
  const rows = storedRows(apiKey, form);
  return sendJson(res, 200, historyPage(form, rows, rows.length === 0 ? 0 : 1));
}

function respondLedger(res, form) {
  const { posts, reads, notices, scrobbles } = ledgerOf(form.get("api_key"));
  return sendJson(res, 200, { posts, reads, notices, stored: scrobbles.length });
}

const AUTH_METHODS = ["auth.getToken", "auth.getSession"];
const SIGNED_WRITES = {
  "track.scrobble": respondScrobble,
  "track.updateNowPlaying": respondNowPlaying,
};
const UNSIGNED_METHODS = {
  "user.getRecentTracks": respondHistory,
  "simulation.ledger": respondLedger,
};

const server = http.createServer((req, res) => {
  let raw = "";
  req.on("data", (chunk) => (raw += chunk));
  req.on("end", () => {
    // @endpoint POST /
    if (req.method !== "POST" || req.url !== "/") {
      return sendJson(res, 501, { error: "Only a form POST to the root is modeled." });
    }
    const form = new URLSearchParams(raw);
    const method = form.get("method");
    const unsigned = UNSIGNED_METHODS[method];
    if (![...AUTH_METHODS, ...Object.keys(SIGNED_WRITES)].includes(method) && !unsigned) {
      return sendApiError(res, 3, "Invalid method");
    }
    if (form.get("format") !== "json") {
      return sendJson(res, 501, { error: "Only format=json is modeled." });
    }
    if (unsigned) {
      return unsigned(res, form);
    }
    if (form.get("api_sig") !== signature(form)) {
      return sendApiError(res, 13, "Invalid method signature");
    }
    return (SIGNED_WRITES[method] || respond)(res, form);
  });
});

server.listen(PORT, "127.0.0.1");
