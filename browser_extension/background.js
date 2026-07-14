/*
 * Samsara Show Numbers -- background service worker.
 *
 * Owns the single WebSocket connection to the local Samsara bridge
 * (ws://127.0.0.1:47831), does the hello/hello_ack handshake, and relays
 * show_hints/select/dismiss requests to the active tab's content script,
 * forwarding its response back over the socket with the original
 * requestId. Also forwards unsolicited "dismissed" events (in-page Escape)
 * from the content script back to Samsara.
 *
 * MV3 service workers can be suspended when idle; a chrome.alarms-based
 * periodic wake keeps reconnection attempts happening even across a
 * suspend/resume cycle. This is a known, documented v1 simplification --
 * a suspended worker will not reconnect until its next wake, not
 * instantly.
 */

var BRIDGE_URL = "ws://127.0.0.1:47831";
var ALARM_NAME = "samsara-bridge-keepalive";

var ws = null;
var token = null;
var connected = false;
var reconnectDelayMs = 1000;
var MAX_RECONNECT_DELAY_MS = 10000;
var reconnectTimer = null;

function log() {
  var args = ["[SamsaraBridge]"].concat(Array.prototype.slice.call(arguments));
  console.log.apply(console, args);
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(function () {
    reconnectTimer = null;
    connect();
  }, reconnectDelayMs);
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, MAX_RECONNECT_DELAY_MS);
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  try {
    ws = new WebSocket(BRIDGE_URL);
  } catch (e) {
    log("connect failed:", e && e.message);
    scheduleReconnect();
    return;
  }

  ws.onopen = function () {
    log("connected, sending hello");
    ws.send(JSON.stringify({ type: "hello" }));
  };

  ws.onmessage = function (event) {
    var msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      log("received malformed message, ignoring");
      return;
    }
    handleServerMessage(msg);
  };

  ws.onclose = function () {
    log("disconnected");
    connected = false;
    token = null;
    scheduleReconnect();
  };

  ws.onerror = function () {
    // onclose fires after onerror for a failed connection; no separate
    // handling needed here beyond logging without the token/any payload.
    log("connection error");
  };
}

function handleServerMessage(msg) {
  if (!msg || typeof msg.type !== "string") return;

  if (msg.type === "hello_ack") {
    token = msg.token;
    connected = true;
    reconnectDelayMs = 1000;
    log("handshake complete, hint count log deferred to per-request logging");
    return;
  }

  if (msg.type === "show_hints" || msg.type === "select" || msg.type === "dismiss") {
    relayToActiveTab(msg);
    return;
  }
}

function sendToServer(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  var withToken = Object.assign({ token: token }, payload);
  ws.send(JSON.stringify(withToken));
}

function relayToActiveTab(msg) {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    if (!tabs || !tabs.length) {
      sendToServer({ type: "hints_unavailable", requestId: msg.requestId, reason: "no_active_tab" });
      return;
    }
    var tabId = tabs[0].id;
    chrome.tabs.sendMessage(tabId, msg, function (response) {
      if (chrome.runtime.lastError || !response) {
        // No content script in this tab -- restricted page (chrome://,
        // the Web Store, a PDF viewer, etc.) or the tab hasn't finished
        // loading yet. Reported visibly to Samsara, not silently dropped.
        sendToServer({
          type: "hints_unavailable",
          requestId: msg.requestId,
          reason: "no_content_script",
        });
        return;
      }
      response.requestId = msg.requestId;
      sendToServer(response);
    });
  });
}

// Unsolicited messages from a content script (in-page Escape dismissal).
chrome.runtime.onMessage.addListener(function (msg) {
  if (msg && msg.type === "dismissed") {
    sendToServer({ type: "dismissed" });
  }
  return false;
});

chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === ALARM_NAME && !connected) {
    connect();
  }
});

chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
connect();
