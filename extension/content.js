if (!window.__consensusGuardIndicatorInstalled) {
  window.__consensusGuardIndicatorInstalled = true;

  const BADGE_ID = "consensusguard-session-indicator";
  const STATUS_TYPES = new Set([
    "starting",
    "capture",
    "upload",
    "match",
    "no-match",
    "inconclusive",
    "error",
    "stopped"
  ]);

  function updateBadge(message, active = false, statusType = "capture") {
    let badge = document.getElementById(BADGE_ID);
    if (!badge) {
      badge = document.createElement("div");
      badge.id = BADGE_ID;
      badge.setAttribute("role", "status");
      badge.setAttribute("aria-live", "polite");
      (document.body || document.documentElement).appendChild(badge);
    }

    badge.textContent = message;
    badge.dataset.active = String(active);
    badge.dataset.status = STATUS_TYPES.has(statusType) ? statusType : "capture";
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.target !== "content" || message.type !== "CAPTURE_STATUS") {
      return false;
    }

    const active = Boolean(message.captureActive);
    updateBadge(
      message.status || "ConsensusGuard tab-audio capture",
      active,
      message.statusType
    );
    sendResponse({ ok: true, captureActive: active });
    return false;
  });

  chrome.runtime.sendMessage({
    target: "background",
    type: "CONTENT_READY",
    hostname: location.hostname
  }).catch(() => {});
}
