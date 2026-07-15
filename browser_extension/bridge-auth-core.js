/* Pure pairing-protocol helpers shared by the MV3 worker and Node tests. */
(function (root, factory) {
  var api = factory(root);
  root.SamsaraBridgeAuth = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  function bytesToBase64Url(bytes) {
    var binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    var encoded;
    if (typeof btoa === "function") encoded = btoa(binary);
    else encoded = Buffer.from(bytes).toString("base64");
    return encoded.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function randomNonce(cryptoApi) {
    var api = cryptoApi || root.crypto;
    if (!api || typeof api.getRandomValues !== "function") {
      throw new Error("secure random generator unavailable");
    }
    var bytes = new Uint8Array(32);
    api.getRandomValues(bytes);
    return bytesToBase64Url(bytes);
  }

  async function proof(secret, label, parts, cryptoApi) {
    var api = cryptoApi || root.crypto;
    if (!api || !api.subtle) throw new Error("Web Crypto unavailable");
    if (typeof secret !== "string" || secret.length < 40) {
      throw new Error("invalid pairing secret");
    }
    var encoder = new TextEncoder();
    var key = await api.subtle.importKey(
      "raw",
      encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
    var message = [label].concat(parts || []).join(":");
    var signature = new Uint8Array(
      await api.subtle.sign("HMAC", key, encoder.encode(message))
    );
    return Array.from(signature, function (value) {
      return value.toString(16).padStart(2, "0");
    }).join("");
  }

  function equalProof(left, right) {
    if (typeof left !== "string" || typeof right !== "string") return false;
    if (left.length !== right.length) return false;
    var difference = 0;
    for (var i = 0; i < left.length; i++) {
      difference |= left.charCodeAt(i) ^ right.charCodeAt(i);
    }
    return difference === 0;
  }

  async function verifyServerAck(secret, serverNonce, clientNonce, message, cryptoApi) {
    if (
      !message ||
      message.type !== "hello_ack" ||
      typeof message.token !== "string" ||
      typeof message.serverNonce !== "string" ||
      typeof message.proof !== "string" ||
      message.serverNonce !== serverNonce ||
      typeof clientNonce !== "string"
    ) return false;
    var expected = await proof(
      secret, "server-v1", [serverNonce, clientNonce], cryptoApi
    );
    return equalProof(expected, message.proof);
  }

  function commandAllowed(connected, messageType) {
    return !!connected && (
      messageType === "show_hints" ||
      messageType === "select" ||
      messageType === "dismiss"
    );
  }

  return {
    randomNonce: randomNonce,
    proof: proof,
    equalProof: equalProof,
    verifyServerAck: verifyServerAck,
    commandAllowed: commandAllowed,
  };
});
