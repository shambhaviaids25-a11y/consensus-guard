const BACKEND_OPENAPI_URL = "http://localhost:8000/openapi.json";
const MEETING_API_BASE = "http://localhost:8000/api/meeting";
const OFFSCREEN_URL = "offscreen.html";
const SUPPORTED_ROLES = new Set(["CFO", "COO", "VP", "HOL"]);
const DEFAULT_STATE = {
  backendConnected: false,
  backendStatus: "Not connected",
  scanSessionActive: false,
  scanStatus: "Not started",
  captureActive: false,
  role: null,
  tabId: null,
  identityStatus: "Not started",
  authenticityStatus: "Not started",
  videoAnalysisStatus: "Not started",
  quorumStatus: "Not started",
  latestVerification: null,
  latestVideoVerification: null
};

async function getState() {
  const stored = await chrome.storage.local.get("consensusGuardState");
  return {
    ...DEFAULT_STATE,
    ...(stored.consensusGuardState || {})
  };
}

async function saveState(state) {
  await chrome.storage.local.set({ consensusGuardState: state });
  chrome.runtime.sendMessage({
    target: "popup",
    type: "STATE_UPDATED",
    state
  }).catch(() => { });
  return state;
}

function isPopupSender(sender) {
  return sender.url === chrome.runtime.getURL("popup.html");
}

function isOffscreenSender(sender) {
  return sender.url === chrome.runtime.getURL(OFFSCREEN_URL);
}

function isSupportedMeetingUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") {
      return false;
    }

    return url.hostname === "meet.google.com"
      || url.hostname === "teams.microsoft.com"
      || url.hostname === "teams.live.com"
      || url.hostname === "zoom.us"
      || url.hostname.endsWith(".zoom.us");
  } catch {
    return false;
  }
}

async function updateTabIndicator(tabId, status, active) {
  if (typeof tabId !== "number") {
    return;
  }

  try {
    await chrome.tabs.sendMessage(tabId, {
      target: "content",
      type: "CAPTURE_STATUS",
      status,
      captureActive: active
    });
  } catch {
    // The tab may not have a content script yet; the popup remains authoritative.
  }
}

async function syncQuorumWithBackend(meetingId, role, updates) {
  if (!role) {
    return null;
  }
  const cleanMeetingId = (meetingId || "CG-LIVE-MEETING").trim();

  try {
    await fetch(`${MEETING_API_BASE}/participant/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        meeting_id: cleanMeetingId,
        role,
        ...updates
      })
    });

    const evalResp = await fetch(`${MEETING_API_BASE}/quorum/evaluate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        meeting_id: cleanMeetingId,
        require_audio_authenticity: false,
        require_video_authenticity: true,
        require_secondary_confirmation: false,
        require_voice_identity: false,
        require_challenge: false
      })
    });
    if (!evalResp.ok) {
      return null;
    }
    return await evalResp.json();
  } catch {
    return null;
  }
}

async function checkBackend() {
  const current = await getState();
  await saveState({ ...current, backendStatus: "Checking..." });

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 4000);

  try {
    const response = await fetch(BACKEND_OPENAPI_URL, {
      method: "GET",
      cache: "no-store",
      signal: controller.signal
    });
    if (!response.ok) {
      throw new Error(`Backend returned HTTP ${response.status}.`);
    }

    const openApi = await response.json();
    if (typeof openApi.openapi !== "string" || !openApi.paths) {
      throw new Error("Backend did not return a valid OpenAPI document.");
    }

    const latest = await getState();
    return await saveState({
      ...latest,
      backendConnected: true,
      backendStatus: "Connected",
      backendError: null
    });
  } catch (error) {
    const latest = await getState();
    return await saveState({
      ...latest,
      backendConnected: false,
      backendStatus: "Not connected",
      backendError: error.name === "AbortError"
        ? "Backend check timed out."
        : error.message
    });
  } finally {
    clearTimeout(timeoutId);
  }
}

async function ensureOffscreenDocument() {
  if (await chrome.offscreen.hasDocument()) {
    return;
  }

  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ["USER_MEDIA"],
    justification: "Capture user-started audio and video from a supported meeting tab and upload short verification segments."
  });
}

async function prepareScan(role) {
  if (!SUPPORTED_ROLES.has(role)) {
    return { ok: false, error: "Select CFO, COO, VP, or HOL first." };
  }

  const state = await getState();
  if (!state.backendConnected) {
    return { ok: false, error: "Connect to the backend before starting a scan." };
  }
  if (state.scanSessionActive) {
    if (await chrome.offscreen.hasDocument()) {
      return { ok: true, state };
    }
    await saveState({
      ...state,
      scanSessionActive: false,
      scanStatus: "The previous capture session ended unexpectedly.",
      captureActive: false,
      role: null,
      tabId: null
    });
  }

  const [tab] = await chrome.tabs.query({
    active: true,
    lastFocusedWindow: true
  });
  if (!tab || typeof tab.id !== "number" || !isSupportedMeetingUrl(tab.url || "")) {
    return {
      ok: false,
      error: "Open a supported Google Meet, Microsoft Teams, or Zoom meeting tab first."
    };
  }

  try {
    const streamId = await chrome.tabCapture.getMediaStreamId({
      targetTabId: tab.id
    });
    await ensureOffscreenDocument();

    const offscreenResponse = await chrome.runtime.sendMessage({
      target: "offscreen",
      type: "CAPTURE_START",
      streamId,
      role,
      tabId: tab.id
    });
    if (!offscreenResponse || !offscreenResponse.ok) {
      throw new Error(offscreenResponse?.error || "Could not start tab capture.");
    }

    const latest = await getState();
    const nextState = await saveState({
      ...latest,
      scanSessionActive: true,
      scanStatus: `Capturing tab audio & video for ${role}; recording 10-second segments.`,
      captureActive: true,
      role,
      tabId: tab.id,
      identityStatus: "Waiting for first segment",
      authenticityStatus: "Not checked",
      videoAnalysisStatus: "Scanning video...",
      latestVerification: null,
      latestVideoVerification: null
    });
    await updateTabIndicator(tab.id, `Capturing meeting tab audio & video for ${role}`, true);
    return { ok: true, state: nextState };
  } catch (error) {
    if (await chrome.offscreen.hasDocument()) {
      await chrome.runtime.sendMessage({
        target: "offscreen",
        type: "CAPTURE_STOP"
      }).catch(() => { });
      await chrome.offscreen.closeDocument().catch(() => { });
    }

    await updateTabIndicator(tab.id, "Capture could not start", false);
    const latest = await getState();
    await saveState({
      ...latest,
      scanSessionActive: false,
      scanStatus: "Capture could not start.",
      captureActive: false,
      role: null,
      tabId: null,
      videoAnalysisStatus: "Not started"
    });
    return {
      ok: false,
      error: `Could not start tab capture: ${error.message}`
    };
  }
}

async function stopScan(reason = "Capture stopped by user.") {
  const state = await getState();
  if (await chrome.offscreen.hasDocument()) {
    await chrome.runtime.sendMessage({
      target: "offscreen",
      type: "CAPTURE_STOP"
    }).catch(() => { });
    await chrome.offscreen.closeDocument().catch(() => { });
  }

  await updateTabIndicator(state.tabId, reason, false);
  const latest = await getState();
  const nextState = await saveState({
    ...latest,
    scanSessionActive: false,
    scanStatus: reason,
    captureActive: false,
    role: null,
    tabId: null
  });
  return { ok: true, state: nextState };
}

async function handleOffscreenEvent(message) {
  const state = await getState();
  const meetingId = `CG-TAB-${state.tabId || "LIVE"}`;

  if (message.type === "CAPTURE_PROGRESS") {
    const nextState = await saveState({
      ...state,
      scanSessionActive: true,
      captureActive: true,
      scanStatus: message.status
    });
    await updateTabIndicator(state.tabId, message.status, true);
    return { ok: true, state: nextState };
  }

  if (message.type === "VIDEO_VERIFICATION_RESPONSE") {
    const response = message.response || {};
    const verdict = response.verdict;
    const confidencePct = typeof response.confidence === "number"
      ? ` (${Math.round(response.confidence * 100)}%)`
      : "";

    let videoAnalysisStatus = "Inconclusive";
    if (verdict === "no_suspicious_indicators") {
      videoAnalysisStatus = `Authentic${confidencePct}`;
    } else if (verdict === "suspicious_indicators") {
      videoAnalysisStatus = `Suspicious${confidencePct}`;
    }

    const activeRole = response.role || state.role;
    const quorumReport = await syncQuorumWithBackend(meetingId, activeRole, {
      video_status: verdict || "inconclusive",
      approved: verdict === "no_suspicious_indicators"
    });

    const quorumStatus = quorumReport
      ? `${quorumReport.decision} (${quorumReport.verified_count}/${quorumReport.quorum_required})`
      : state.quorumStatus;

    const nextState = await saveState({
      ...state,
      videoAnalysisStatus,
      quorumStatus,
      latestVideoVerification: {
        role: activeRole,
        segmentIndex: message.segmentIndex,
        httpStatus: message.httpStatus,
        response,
        receivedAt: new Date().toISOString()
      }
    });
    return { ok: true, state: nextState };
  }

  if (message.type === "VERIFICATION_RESPONSE") {
    const response = message.response;
    const decision = response && response.identity_status;
    const successfulResponse = message.httpStatus >= 200 && message.httpStatus < 300;
    const identityStatus = successfulResponse && decision === "match"
      ? "Match"
      : successfulResponse && decision === "no_match"
        ? "No match"
        : "Inconclusive";

    const activeRole = response?.participant || state.role;
    const quorumReport = await syncQuorumWithBackend(meetingId, activeRole, {
      voice_identity_status: decision || "inconclusive",
      audio_authenticity_status: response?.audio_authenticity_status || "not_checked",
      eidos_request_id: response?.request_id || null
    });

    const quorumStatus = quorumReport
      ? `${quorumReport.decision} (${quorumReport.verified_count}/${quorumReport.quorum_required})`
      : state.quorumStatus;

    const nextState = await saveState({
      ...state,
      identityStatus,
      authenticityStatus: response?.audio_authenticity_status || "Not provided",
      quorumStatus,
      latestVerification: {
        role: activeRole,
        segmentIndex: message.segmentIndex,
        httpStatus: message.httpStatus,
        response,
        receivedAt: new Date().toISOString()
      },
      scanStatus: state.captureActive
        ? `Segment ${message.segmentIndex} response received for ${state.role}.`
        : state.scanStatus
    });
    await updateTabIndicator(
      state.tabId,
      `Latest ${state.role} verification: ${identityStatus} | Video: ${state.videoAnalysisStatus} | Quorum: ${quorumStatus}`,
      state.captureActive
    );
    return { ok: true, state: nextState };
  }

  if (message.type === "UPLOAD_FAILED") {
    const nextState = await saveState({
      ...state,
      backendConnected: message.backendUnavailable ? false : state.backendConnected,
      backendStatus: message.backendUnavailable ? "Not connected" : state.backendStatus,
      backendError: message.backendUnavailable
        ? "The verification request could not reach the backend."
        : state.backendError,
      identityStatus: "Inconclusive",
      latestVerification: {
        role: state.role,
        segmentIndex: message.segmentIndex,
        transportError: message.error,
        response: null,
        receivedAt: new Date().toISOString()
      },
      scanStatus: `Segment ${message.segmentIndex} was not verified; capture continues.`
    });
    return { ok: true, state: nextState };
  }

  if (message.type === "CAPTURE_ERROR") {
    const nextState = await saveState({
      ...state,
      scanSessionActive: false,
      captureActive: false,
      scanStatus: `Capture stopped: ${message.error}`,
      identityStatus: state.latestVerification ? state.identityStatus : "Inconclusive"
    });
    await updateTabIndicator(state.tabId, "Capture stopped after a capture error", false);
    await chrome.offscreen.closeDocument().catch(() => { });
    return { ok: true, state: nextState };
  }

  return { ok: false, error: "Unsupported offscreen event." };
}

async function handleMessage(message, sender) {
  if (message.type === "GET_STATE") {
    return { ok: true, state: await getState() };
  }

  if (message.type === "CHECK_BACKEND") {
    if (!isPopupSender(sender)) {
      return { ok: false, error: "Backend checks can only be started from the popup." };
    }
    return { ok: true, state: await checkBackend() };
  }

  if (message.type === "START_SCAN") {
    if (!isPopupSender(sender)) {
      return { ok: false, error: "Capture can only be started from the popup." };
    }
    return prepareScan(message.role);
  }

  if (message.type === "STOP_SCAN") {
    if (!isPopupSender(sender)) {
      return { ok: false, error: "Capture can only be stopped from the popup." };
    }
    return stopScan();
  }

  if (message.type === "CONTENT_READY") {
    return { ok: true };
  }

  if ([
    "CAPTURE_PROGRESS",
    "VERIFICATION_RESPONSE",
    "VIDEO_VERIFICATION_RESPONSE",
    "UPLOAD_FAILED",
    "CAPTURE_ERROR"
  ].includes(message.type)) {
    if (!isOffscreenSender(sender)) {
      return { ok: false, error: "Capture events can only come from the offscreen document." };
    }
    return handleOffscreenEvent(message);
  }

  return { ok: false, error: "Unsupported extension message." };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.target !== "background") {
    return false;
  }

  handleMessage(message, sender)
    .then(sendResponse)
    .catch(error => sendResponse({ ok: false, error: error.message }));
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  getState()
    .then(state => state.tabId === tabId
      ? stopScan("Meeting tab closed; capture stopped.")
      : undefined)
    .catch(() => { });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!changeInfo.url) {
    return;
  }

  getState()
    .then(state => {
      if (state.tabId === tabId && !isSupportedMeetingUrl(changeInfo.url)) {
        return stopScan("Meeting tab navigated away; capture stopped.");
      }
      return undefined;
    })
    .catch(() => { });
});

chrome.runtime.onInstalled.addListener(async () => {
  const stored = await chrome.storage.local.get("consensusGuardState");
  if (!stored.consensusGuardState) {
    await chrome.storage.local.set({ consensusGuardState: DEFAULT_STATE });
  }
});