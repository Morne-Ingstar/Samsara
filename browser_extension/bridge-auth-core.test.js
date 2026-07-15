"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHmac, webcrypto } = require("node:crypto");
const auth = require("./bridge-auth-core.js");

const SECRET = "test-pairing-secret-0123456789-abcdefghijklmnopqrstuvwxyz";
const CLIENT_NONCE = "client-nonce-0123456789-abcdefghijklmnopqrstuvwxyz";
const SERVER_NONCE = "server-nonce-0123456789-abcdefghijklmnopqrstuvwxyz";

function expected(label, parts) {
  return createHmac("sha256", SECRET)
    .update([label].concat(parts).join(":"))
    .digest("hex");
}

test("proof matches the Python protocol's HMAC-SHA256 wire format", async () => {
  assert.equal(
    await auth.proof(
      SECRET, "client-v1", [SERVER_NONCE, CLIENT_NONCE], webcrypto
    ),
    expected("client-v1", [SERVER_NONCE, CLIENT_NONCE])
  );
});

test("server acknowledgement requires the paired secret", async () => {
  const message = {
    type: "hello_ack",
    token: "ephemeral-token",
    serverNonce: SERVER_NONCE,
    proof: expected("server-v1", [SERVER_NONCE, CLIENT_NONCE]),
  };
  assert.equal(
    await auth.verifyServerAck(
      SECRET, SERVER_NONCE, CLIENT_NONCE, message, webcrypto
    ),
    true
  );
  assert.equal(
    await auth.verifyServerAck(
      "wrong-pairing-secret-0123456789-abcdefghijklmnopqrstuvwxyz",
      SERVER_NONCE,
      CLIENT_NONCE,
      message,
      webcrypto
    ),
    false
  );
});

test("commands are rejected until server authentication completes", () => {
  for (const type of ["show_hints", "select", "dismiss"]) {
    assert.equal(auth.commandAllowed(false, type), false);
    assert.equal(auth.commandAllowed(true, type), true);
  }
  assert.equal(auth.commandAllowed(true, "hello_ack"), false);
});

test("nonces use 32 bytes of secure randomness", () => {
  const nonce = auth.randomNonce(webcrypto);
  assert.match(nonce, /^[A-Za-z0-9_-]{43}$/);
});
