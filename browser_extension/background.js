/* Samsara Show Numbers -- mutually authenticated loopback bridge. */
importScripts("bridge-auth-core.js");

var BRIDGE_URL = "ws://127.0.0.1:47831";
var ALARM_NAME = "samsara-bridge-keepalive";
var AUTH_FILE = "pairing-auth.json";

var ws = null;
var token = null;
var pairingSecret = null;
var serverNonce = null;
var clientNonce = null;
var serverVerified = false;
var connected = false;
var connectInFlight = false;
var reconnectDelayMs = 1000;
var MAX_RECONNECT_DELAY_MS = 10000;
var reconnectTimer = null;

function log() {
  var args = ["[SamsaraBridge]"].concat(Array.prototype.slice.call(arguments));
  console.log.apply(console, args);
}

function setPairingFailure(message) {
  log("PAIRING REQUIRED:", message);
  if (chrome.action) {
    chrome.action.setBadgeBackgroundColor({ color: "#B91C1C" });
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setTitle({ title: "Samsara Show Numbers: pairing failed — reload the extension directory shown in the Samsara log" });
  }
}

function clearPairingFailure() {
  if (chrome.action) {
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "Samsara Show Numbers (paired)" });
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(function () {
    reconnectTimer = null;
    connect();
  }, reconnectDelayMs);
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, MAX_RECONNECT_DELAY_MS);
}

async function loadPairingSecret() {
  var response = await fetch(chrome.runtime.getURL(AUTH_FILE), { cache: "no-store" });
  if (!response.ok) throw new Error("pairing file missing");
  var payload = await response.json();
  if (!payload || payload.version !== 1 || typeof payload.secret !== "string" || payload.secret.length < 40) {
    throw new Error("pairing file invalid");
  }
  return payload.secret;
}

async function connect() {
  if (connectInFlight) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  connectInFlight = true;
  try {
    pairingSecret = await loadPairingSecret();
  } catch (error) {
    setPairingFailure(error && error.message ? error.message : "could not load pairing secret");
    connectInFlight = false;
    scheduleReconnect();
    return;
  }

  try {
    ws = new WebSocket(BRIDGE_URL);
  } catch (error) {
    log("connect failed:", error && error.message);
    connectInFlight = false;
    scheduleReconnect();
    return;
  }
  var socket = ws;

  socket.onopen = async function () {
    log("connected, waiting for authenticated challenge");
  };

  socket.onmessage = function (event) {
    var msg;
    try {
      msg = JSON.parse(event.data);
    } catch (error) {
      log("received malformed message, closing");
      socket.close();
      return;
    }
    handleServerMessage(socket, msg);
  };

  socket.onclose = function () {
    if (socket === ws) ws = null;
    connected = false;
    connectInFlight = false;
    token = null;
    serverNonce = null;
    clientNonce = null;
    serverVerified = false;
    pairingSecret = null; // reload on reconnect so secret rotation recovers
    scheduleReconnect();
  };

  socket.onerror = function () {
    log("connection error");
  };
  connectInFlight = false;
}

async function handleServerMessage(socket, msg) {
  if (!msg || typeof msg.type !== "string" || socket !== ws) return;

  if (msg.type === "challenge") {
    if (
      connected ||
      serverNonce ||
      typeof msg.serverNonce !== "string" ||
      msg.serverNonce.length < 20 ||
      !pairingSecret
    ) {
      setPairingFailure("malformed or repeated server challenge");
      socket.close();
      return;
    }
    try {
      serverNonce = msg.serverNonce;
      clientNonce = SamsaraBridgeAuth.randomNonce();
      var clientProof = await SamsaraBridgeAuth.proof(
        pairingSecret, "client-v1", [serverNonce, clientNonce]
      );
      if (socket !== ws || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({
        type: "hello",
        serverNonce: serverNonce,
        clientNonce: clientNonce,
        proof: clientProof,
      }));
      log("sent challenge-bound authenticated hello");
    } catch (error) {
      setPairingFailure("could not create client proof");
      socket.close();
    }
    return;
  }

  if (msg.type === "hello_ack") {
    if (
      connected ||
      typeof msg.token !== "string" ||
      typeof msg.serverNonce !== "string" ||
      typeof msg.proof !== "string" ||
      !serverNonce ||
      !clientNonce ||
      !pairingSecret
    ) {
      setPairingFailure("malformed server proof");
      socket.close();
      return;
    }
    try {
      if (!await SamsaraBridgeAuth.verifyServerAck(
        pairingSecret, serverNonce, clientNonce, msg
      )) {
        setPairingFailure("server proof mismatch — refusing local impersonator");
        socket.close();
        return;
      }
    } catch (error) {
      setPairingFailure("could not verify server proof");
      socket.close();
      return;
    }
    token = msg.token;
    // Raw send is intentional: connected remains false until ready is queued,
    // so no server command can race the asynchronous proof verification.
    socket.send(JSON.stringify({ type: "ready", token: token }));
    serverVerified = true;
    return;
  }

  if (msg.type === "ready_ack") {
    if (!serverVerified || !token) {
      setPairingFailure("unexpected ready acknowledgement");
      socket.close();
      return;
    }
    connected = true;
    reconnectDelayMs = 1000;
    clearPairingFailure();
    log("mutually authenticated handshake complete");
    return;
  }

  // Commands received before server authentication are always ignored.
  if (!connected) {
    setPairingFailure("unauthenticated command rejected");
    socket.close();
    return;
  }
  if (SamsaraBridgeAuth.commandAllowed(connected, msg.type)) {
    relayToActiveTab(msg);
  }
}

function sendToServer(payload) {
  if (!connected || !token || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(Object.assign({ token: token }, payload)));
}

function relayToActiveTab(msg) {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    if (!tabs || !tabs.length) {
      sendToServer({ type: "hints_unavailable", requestId: msg.requestId, reason: "no_active_tab" });
      return;
    }
    chrome.tabs.sendMessage(tabs[0].id, msg, function (response) {
      if (chrome.runtime.lastError || !response) {
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

chrome.runtime.onMessage.addListener(function (msg) {
  if (msg && msg.type === "dismissed") sendToServer({ type: "dismissed" });
  return false;
});

chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === ALARM_NAME && !connected) connect();
});
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
connect();
