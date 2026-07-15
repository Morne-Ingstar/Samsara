/*
 * Samsara Show Numbers -- content script glue.
 *
 * Renders numbered hint labels inside a Shadow DOM host (avoids colliding
 * with page CSS), keeps them positioned correctly across scroll/resize, and
 * performs selection (focus for text-entry, synthetic click for buttons/
 * links). All coordinates come from getBoundingClientRect(), which is
 * already in CSS pixels -- browser zoom and OS DPI scaling never need
 * separate correction because we never read physical/device pixels at all.
 *
 * Talks to background.js via chrome.runtime messaging; background.js owns
 * the actual WebSocket connection to the local Samsara bridge.
 */
(function () {
  "use strict";

  // The spoken-number path supports 1..99. Fifty was too low on dense apps:
  // Gmail's repeated checkbox/star controls left only a handful of message
  // rows reachable even after semantic ranking.
  var MAX_HINTS = 99;

  var _hints = []; // [{number, el, rect}], populated once per show_hints call
  var _shadowHost = null;
  var _shadowRoot = null;
  var _reposRaf = null;

  function clearHints() {
    _hints = [];
    if (_shadowHost && _shadowHost.parentNode) {
      _shadowHost.parentNode.removeChild(_shadowHost);
    }
    _shadowHost = null;
    _shadowRoot = null;
    window.removeEventListener("scroll", onScrollOrResize, true);
    window.removeEventListener("resize", onScrollOrResize, true);
    document.removeEventListener("keydown", onKeyDown, true);
  }

  function ensureShadowHost() {
    if (_shadowHost) return;
    _shadowHost = document.createElement("div");
    _shadowHost.id = "samsara-show-numbers-host";
    _shadowHost.style.cssText =
      "position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;";
    _shadowRoot = _shadowHost.attachShadow({ mode: "open" });
    var style = document.createElement("style");
    style.textContent =
      ".pill{position:fixed;font:600 11px/16px system-ui,sans-serif;" +
      "background:#1a1a1f;color:#5EEAD4;border:1px solid rgba(94,234,212,0.6);" +
      "border-radius:4px;padding:0 4px;min-width:14px;text-align:center;" +
      "pointer-events:none;white-space:nowrap;}";
    _shadowRoot.appendChild(style);
    document.documentElement.appendChild(_shadowHost);
  }

  function renderLabels() {
    ensureShadowHost();
    // Clear previous pills (keep the <style> node).
    var pills = _shadowRoot.querySelectorAll(".pill");
    for (var i = 0; i < pills.length; i++) pills[i].remove();

    var vw = window.innerWidth;
    var vh = window.innerHeight;
    for (var j = 0; j < _hints.length; j++) {
      var hint = _hints[j];
      var rect = window.SamsaraCore.rectOf(hint.el);
      hint.rect = rect;
      var pos = window.SamsaraCore.computeLabelPosition(rect, vw, vh);
      var pill = document.createElement("div");
      pill.className = "pill";
      pill.style.left = pos.left + "px";
      pill.style.top = pos.top + "px";
      pill.textContent = String(hint.number);
      _shadowRoot.appendChild(pill);
    }
  }

  function onScrollOrResize() {
    if (_reposRaf) return;
    _reposRaf = requestAnimationFrame(function () {
      _reposRaf = null;
      if (_hints.length) renderLabels();
    });
  }

  function onKeyDown(e) {
    if (e.key === "Escape" && _hints.length) {
      clearHints();
      chrome.runtime.sendMessage({ type: "dismissed" });
    }
  }

  function handleShowHints() {
    clearHints();
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var candidates = window.SamsaraCore.discoverCandidates(document, vw, vh);
    if (candidates.length === 0) {
      return { type: "hints_unavailable", reason: "no_candidates" };
    }
    var center = { x: vw / 2, y: vh / 2 };
    var selected = window.SamsaraCore.prioritize(candidates, center, MAX_HINTS);

    _hints = selected.map(function (c, i) {
      return { number: i + 1, el: c.el, rect: c.rect, kind: c.kind };
    });

    renderLabels();
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize, true);
    document.addEventListener("keydown", onKeyDown, true);

    return {
      type: "hints",
      hints: _hints.map(function (h) {
        return { index: h.number, kind: h.kind, rect: h.rect };
      }),
    };
  }

  function dispatchSyntheticClick(el, opts) {
    var init = {
      bubbles: true,
      cancelable: true,
      view: window,
      ctrlKey: !!(opts && opts.ctrlKey),
      shiftKey: !!(opts && opts.shiftKey),
      altKey: !!(opts && opts.altKey),
      button: opts && opts.button != null ? opts.button : 0,
    };
    el.dispatchEvent(new MouseEvent("mousedown", init));
    el.dispatchEvent(new MouseEvent("mouseup", init));
    el.dispatchEvent(new MouseEvent("click", init));
  }

  function handleSelect(msg) {
    var hint = null;
    for (var i = 0; i < _hints.length; i++) {
      if (_hints[i].number === msg.number) {
        hint = _hints[i];
        break;
      }
    }
    if (!hint) {
      return { type: "selection_result", ok: false, reason: "not_found" };
    }

    var el = hint.el;
    el.scrollIntoView({ block: "nearest", inline: "nearest" });

    var action = msg.action || "click";
    var textKinds = { input: true, textbox: true, contenteditable: true };

    if (action === "focus" || (action === "click" && textKinds[hint.kind])) {
      el.focus();
    } else if (action === "doubleclick") {
      dispatchSyntheticClick(el, msg.modifiers);
      dispatchSyntheticClick(el, msg.modifiers);
    } else if (action === "rightclick") {
      dispatchSyntheticClick(el, Object.assign({}, msg.modifiers, { button: 2 }));
    } else {
      dispatchSyntheticClick(el, msg.modifiers);
    }

    clearHints();
    return { type: "selection_result", ok: true };
  }

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (!msg || typeof msg.type !== "string") return false;

    if (msg.type === "show_hints") {
      var result = handleShowHints();
      result.requestId = msg.requestId;
      sendResponse(result);
      return false;
    }
    if (msg.type === "select") {
      var selResult = handleSelect(msg);
      selResult.requestId = msg.requestId;
      sendResponse(selResult);
      return false;
    }
    if (msg.type === "dismiss") {
      clearHints();
      sendResponse({ type: "dismissed", requestId: msg.requestId });
      return false;
    }
    return false;
  });
})();
