const BADGE_ID = "consensusguard-session-indicator";

function updateBadge(message, active = false) {
  let badge = document.getElementById(BADGE_ID);
  if (!badge) {
    badge = document.createElement("div");
    badge.id = BADGE_ID;
    badge.setAttribute("role", "status");
    badge.setAttribute("aria-live", "polite");
    document.documentElement.appendChild(badge);
  }

  badge.textContent = message;
  badge.dataset.active = String(active);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== "content") {
    return false;
  }

  if (message.type === "CAPTURE_STATUS") {
    const active = Boolean(message.captureActive);
    updateBadge(message.status || "ConsensusGuard tab-audio capture", active);
    sendResponse({ ok: true, captureActive: active });
    return false;
  }

  if (message.type === "SCAN_PREPARE") {
    updateBadge("ConsensusGuard capture starting", false);
    sendResponse({ ok: true, captureActive: false });
    return false;
  }

  if (message.type === "SCAN_STOP") {
    updateBadge("ConsensusGuard capture stopped", false);
    sendResponse({ ok: true, captureActive: false });
    return false;
  }

  return false;
});

chrome.runtime.sendMessage({
  target: "background",
  type: "CONTENT_READY",
  hostname: location.hostname
}).catch(() => {});
