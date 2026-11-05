"use strict";

const crypto = require("node:crypto");
const net = require("node:net");

const LOOPBACK_HOST = "127.0.0.1";
const ALLOWED_NAVIGATION_HOSTS = new Set([
  "chatgpt.com",
  "chat.openai.com",
  "auth.openai.com",
  "auth0.openai.com",
  "login.openai.com",
  "accounts.google.com",
  "login.microsoftonline.com",
  "login.live.com",
  "appleid.apple.com",
]);

function createAccessToken() {
  return crypto.randomBytes(48).toString("base64url");
}

function tokenMatches(expected, header) {
  if (typeof expected !== "string" || typeof header !== "string") return false;
  if (!header.startsWith("Bearer ")) return false;
  const supplied = header.slice(7);
  const left = Buffer.from(expected, "utf8");
  const right = Buffer.from(supplied, "utf8");
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function isAllowedNavigation(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }
  return parsed.protocol === "https:" && ALLOWED_NAVIGATION_HOSTS.has(parsed.hostname);
}

function assertLoopbackAddress(address) {
  if (!address) return;
  const normalized = address.startsWith("::ffff:") ? address.slice(7) : address;
  if (!net.isIP(normalized) || normalized !== LOOPBACK_HOST) {
    throw new Error("DocuRandom Chat Bridge rejected a non-loopback client");
  }
}

module.exports = {
  ALLOWED_NAVIGATION_HOSTS,
  LOOPBACK_HOST,
  assertLoopbackAddress,
  createAccessToken,
  isAllowedNavigation,
  tokenMatches,
};
