/*
 * Samsara Show Numbers -- pure DOM-discovery/prioritization logic.
 *
 * Deliberately free of chrome.* and window-only globals so the pure
 * functions (prioritize, computeLabelPosition) can be unit-tested with
 * plain `node --test` against fake {kind, rect} objects, with no browser
 * and no new npm dependency. The DOM-touching functions (isVisible,
 * discoverCandidates) need a real document and are exercised by the
 * Playwright-driven tests instead.
 *
 * Architectural note: the general approach of numbering interactive page
 * elements for voice/keyboard selection is inspired by the Rango browser
 * extension (https://github.com/david-tejada/rango, MIT license, David
 * Martinez Tejada). No code from Rango is copied -- see
 * THIRD_PARTY_NOTICES.md for the full acknowledgement.
 */
(function (root) {
  "use strict";

  // Interactive-element selector. Deliberately excludes anything that lives
  // in browser chrome (tabs/bookmarks/omnibox) by construction: a content
  // script's `document` is only ever the webpage's own DOM.
  var INTERACTIVE_SELECTOR = [
    "a[href]",
    "button",
    "input:not([type=hidden])",
    "textarea",
    "select",
    "[contenteditable]",
    "[contenteditable='']",
    "[role=button]",
    "[role=link]",
    "[role=checkbox]",
    "[role=radio]",
    "[role=menuitem]",
    "[role=menuitemcheckbox]",
    "[role=menuitemradio]",
    "[role=tab]",
    "[role=switch]",
    "[role=combobox]",
    "[role=textbox]",
    "[role=slider]",
    "[role=option]",
  ].join(",");

  var KIND_PRIORITY = {
    input: 0,
    textbox: 0,
    button: 1,
    link: 2,
    select: 3,
    contenteditable: 0,
    aria: 4,
  };

  function classifyKind(el) {
    var tag = el.tagName ? el.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea") return "input";
    if (el.isContentEditable) return "contenteditable";
    if (tag === "button") return "button";
    if (tag === "a") return "link";
    if (tag === "select") return "select";
    var role = el.getAttribute && el.getAttribute("role");
    if (role === "textbox") return "textbox";
    if (role === "button") return "button";
    if (role === "link") return "link";
    return "aria";
  }

  // isDisabled: covers the standard `disabled` property/attribute plus
  // aria-disabled, since many custom-role controls (role=button on a div)
  // don't have a native disabled state at all.
  function isDisabled(el) {
    if (el.disabled) return true;
    var ariaDisabled = el.getAttribute && el.getAttribute("aria-disabled");
    return ariaDisabled === "true";
  }

  // isVisible: needs a real document (getComputedStyle / getBoundingClientRect
  // / checkVisibility). Excludes hidden, zero-sized, disabled, and
  // off-viewport elements per the spec's explicit exclusion list.
  function isVisible(el, viewportWidth, viewportHeight) {
    if (isDisabled(el)) return false;

    if (typeof el.checkVisibility === "function") {
      var ok = el.checkVisibility({
        checkOpacity: true,
        checkVisibilityCSS: true,
      });
      if (!ok) return false;
    } else {
      var style = root.getComputedStyle ? root.getComputedStyle(el) : null;
      if (style) {
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") {
          return false;
        }
        if (parseFloat(style.opacity) === 0) return false;
      }
      if (el.offsetParent === null && style && style.position !== "fixed") {
        return false;
      }
    }

    var rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;

    // Off-viewport exclusion: any intersection with the viewport counts as
    // visible (a partially-scrolled-in element is still actionable).
    if (rect.right <= 0 || rect.bottom <= 0) return false;
    if (rect.left >= viewportWidth || rect.top >= viewportHeight) return false;

    return true;
  }

  function rectOf(el) {
    var r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  }

  // discoverCandidates: needs a real `root_` document/element to query.
  // Returns [{ el, kind, rect }], unfiltered by count -- prioritize() below
  // does the batching/capping as a separate, pure step.
  function discoverCandidates(doc, viewportWidth, viewportHeight) {
    var nodes = doc.querySelectorAll(INTERACTIVE_SELECTOR);
    var out = [];
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!isVisible(el, viewportWidth, viewportHeight)) continue;
      out.push({ el: el, kind: classifyKind(el), rect: rectOf(el) });
    }
    return out;
  }

  // prioritize: PURE function -- operates only on {kind, rect} data, never
  // touches `.el`/`.ref` beyond carrying it through untouched. This is what
  // the zero-dependency Node test exercises directly with fake candidates.
  // Sorts by (a) kind priority (text-entry/buttons/links first), then (b)
  // Euclidean distance from the element's center to the viewport center.
  // maxCount is a parameter specifically so paging (a later milestone) is
  // just a different offset/slice, not a different function.
  function prioritize(candidates, viewportCenter, maxCount) {
    var scored = candidates.map(function (c) {
      var cx = c.rect.x + c.rect.width / 2;
      var cy = c.rect.y + c.rect.height / 2;
      var dx = cx - viewportCenter.x;
      var dy = cy - viewportCenter.y;
      var dist = Math.sqrt(dx * dx + dy * dy);
      var kindRank = KIND_PRIORITY.hasOwnProperty(c.kind) ? KIND_PRIORITY[c.kind] : 5;
      return { c: c, kindRank: kindRank, dist: dist };
    });
    scored.sort(function (a, b) {
      if (a.kindRank !== b.kindRank) return a.kindRank - b.kindRank;
      return a.dist - b.dist;
    });
    return scored.slice(0, maxCount).map(function (s) {
      return s.c;
    });
  }

  // computeLabelPosition: PURE -- given an element rect (viewport-relative
  // CSS pixels, already zoom/DPI-correct because it came from
  // getBoundingClientRect), returns where to place the numbered pill.
  // Clamped so a label near the very edge of the viewport doesn't render
  // off-screen.
  function computeLabelPosition(rect, viewportWidth, viewportHeight) {
    var labelW = 22;
    var labelH = 16;
    var left = rect.x - labelW / 2;
    var top = rect.y - labelH - 2;
    if (top < 0) top = rect.y + 2; // no room above -- place inside top edge instead
    if (left < 0) left = 0;
    if (left + labelW > viewportWidth) left = viewportWidth - labelW;
    if (top + labelH > viewportHeight) top = viewportHeight - labelH;
    return { left: left, top: top };
  }

  var api = {
    INTERACTIVE_SELECTOR: INTERACTIVE_SELECTOR,
    classifyKind: classifyKind,
    isDisabled: isDisabled,
    isVisible: isVisible,
    rectOf: rectOf,
    discoverCandidates: discoverCandidates,
    prioritize: prioritize,
    computeLabelPosition: computeLabelPosition,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.SamsaraCore = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
