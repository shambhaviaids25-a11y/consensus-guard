const BACKEND_OPENAPI_URL = "http://localhost:8000/openapi.json";
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
  latestVerification: null
};

function getVerificationErrorCategory(httpStatus, response, fallback = null) {
  if (typeof fallback === "string" && /^[A-Za-z0-9_.:-]{1,64}$/.test(fallback)) {
    return fallback;
  }

  const errors = Array.isArray(response?.errors) ? response.errors : [];
  for (const entry of errors) {
    const code = entry?.error?.code;
    if (typeof code === "string" && /^[A-Za-z0-9_.:-]{1,64}$/.test(code)) {
      return code;
    }
  }
  if (httpStatus === 402) {
    return "payment_required";
  }
  if (Number.isInteger(httpStatus) && (httpStatus < 200 || httpStatus >= 300)) {
    return `http_${httpStatus}`;
  }
  if (response?.identity_status === "error") {
    return "verification_error";
  }
  if (response && !["match", "no_match"].includes(response.identity_status)) {
    return "missing_identity_decision";
  }
  return "none";
}

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
  }).catch(() => {});
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

async function ensureContentIndicator(tabId) {
  await chrome.scripting.insertCSS({
    target: { tabId },
    files: ["content.css"]
  });
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"]
  });
}

async function updateTabIndicator(tabId, status, active, statusType = "capture") {
  if (typeof tabId !== "number") {
    return;
  }

  const message = {
    target: "content",
    type: "CAPTURE_STATUS",
    status,
    statusType,
    captureActive: active
  };

  try {
    const response = await chrome.tabs.sendMessage(tabId, message);
    if (response?.ok) {
      return;
    }
  } catch {
    // Tabs opened before extension reload may not have the manifest content script.
  }

  try {
    await ensureContentIndicator(tabId);
    await chrome.tabs.sendMessage(tabId, message);
  } catch (error) {
    console.warn("[ConsensusGuard] Could not update meeting-page status indicator.", {
      error: error.message
    });
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
    justification: "Capture user-started audio from a supported meeting tab and upload short verification segments."
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
    await ensureContentIndicator(tab.id);
    await updateTabIndicator(
      tab.id,
      `Scanning started for ${role}; waiting for tab audio.`,
      false,
      "starting"
    );

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
      throw new Error(offscreenResponse?.error || "Could not start tab audio capture.");
    }

    const latest = await getState();
    const nextState = await saveState({
      ...latest,
      scanSessionActive: true,
      scanStatus: `Capturing tab audio for ${role}; recording 10-second segments.`,
      captureActive: true,
      role,
      tabId: tab.id,
      identityStatus: "Waiting for first segment",
      authenticityStatus: "Not checked",
      latestVerification: null
    });
    await updateTabIndicator(
      tab.id,
      `Audio capture active for ${role}.`,
      true,
      "capture"
    );
    return { ok: true, state: nextState };
  } catch (error) {
    if (await chrome.offscreen.hasDocument()) {
      await chrome.runtime.sendMessage({
        target: "offscreen",
        type: "CAPTURE_STOP"
      }).catch(() => {});
      await chrome.offscreen.closeDocument().catch(() => {});
    }

    await updateTabIndicator(
      tab.id,
      `Capture error: ${error.message}`,
      false,
      "error"
    );
    const latest = await getState();
    await saveState({
      ...latest,
      scanSessionActive: false,
      scanStatus: "Capture could not start.",
      captureActive: false,
      role: null,
      tabId: null
    });
    return {
      ok: false,
      error: `Could not start tab audio capture: ${error.message}`
    };
  }
}

async function stopScan(reason = "Capture stopped by user.") {
  const state = await getState();
  if (await chrome.offscreen.hasDocument()) {
    await chrome.runtime.sendMessage({
      target: "offscreen",
      type: "CAPTURE_STOP"
    }).catch(() => {});
    await chrome.offscreen.closeDocument().catch(() => {});
  }

  await updateTabIndicator(state.tabId, reason, false, "stopped");
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

  if (message.type === "CAPTURE_PROGRESS") {
    const nextState = await saveState({
      ...state,
      scanSessionActive: true,
      captureActive: true,
      scanStatus: message.status
    });
    const statusType = message.status.startsWith("Uploading")
      ? "upload"
      : "capture";
    await updateTabIndicator(state.tabId, message.status, true, statusType);
    return { ok: true, state: nextState };
  }

  if (message.type === "VERIFICATION_RESPONSE") {
    const response = message.response;
    const decision = response && response.identity_status;
    const successfulResponse = message.httpStatus >= 200 && message.httpStatus < 300;
    const errorCategory = getVerificationErrorCategory(
      message.httpStatus,
      response
    );
    const invalidAudio = Array.isArray(response?.errors) && response.errors.some(
      entry => entry?.error?.code === "unsupported_format"
        || entry?.error?.code === "file_too_large"
        || (
          entry?.error?.type === "unprocessable_audio"
          && entry?.error?.param === "file"
        )
        || [
          "invalid_audio_file",
          "empty_audio_file",
          "audio_file_too_large",
          "missing_audio_file"
        ].includes(entry?.error?.code)
    );
    const identityStatus = !successfulResponse
      ? invalidAudio
        ? `Invalid audio (HTTP ${message.httpStatus})`
        : `API error (HTTP ${message.httpStatus})`
      : decision === "match"
        ? "Match"
        : decision === "no_match"
          ? "No match"
          : decision === "error"
            ? `Verification error (${errorCategory})`
            : "Inconclusive";
    console.info("[ConsensusGuard] verification_result", {
      selected_role: message.selectedRole || response?.participant || state.role || "unknown",
      segment_index: message.segmentIndex,
      audio_mime_type: message.audioMimeType || "unknown",
      duration_seconds: message.durationSeconds ?? null,
      audio_bytes: Number.isFinite(message.audioBytes) ? message.audioBytes : null,
      audio_empty: Boolean(message.audioEmpty),
      http_status: message.httpStatus,
      identity_status: typeof decision === "string" ? decision : null,
      similarity: Number.isFinite(response?.similarity) ? response.similarity : null,
      error_category: errorCategory
    });
    const nextState = await saveState({
      ...state,
      identityStatus,
      authenticityStatus: response?.audio_authenticity_status || "Not provided",
      latestVerification: {
        role: message.selectedRole || response?.participant || state.role,
        segmentIndex: message.segmentIndex,
        httpStatus: message.httpStatus,
        response,
        receivedAt: new Date().toISOString()
      },
      scanStatus: state.captureActive
        ? `Segment ${message.segmentIndex} response received for ${state.role}.`
        : state.scanStatus
    });
    const indicatorStatusType = !successfulResponse || decision === "error"
      ? "error"
      : decision === "match"
        ? "match"
        : decision === "no_match"
          ? "no-match"
          : "inconclusive";
    const indicatorMessage = !successfulResponse
      ? `Verification error: backend returned HTTP ${message.httpStatus}.`
      : decision === "match"
        ? `MATCH — Eidos matched the selected ${state.role} voice.`
        : decision === "no_match"
          ? `NO MATCH — Eidos did not match the selected ${state.role} voice.`
          : decision === "error"
            ? `Verification error (${errorCategory}).`
            : `INCONCLUSIVE — Eidos returned no identity decision for ${state.role}.`;
    await updateTabIndicator(
      state.tabId,
      indicatorMessage,
      state.captureActive,
      indicatorStatusType
    );
    return { ok: true, state: nextState };
  }

  if (message.type === "UPLOAD_FAILED") {
    const errorCategory = getVerificationErrorCategory(
      message.httpStatus,
      null,
      message.errorCategory || (message.backendUnavailable
        ? "backend_unavailable"
        : message.apiError
          ? "api_error"
          : null)
    );
    console.info("[ConsensusGuard] verification_failed", {
      selected_role: message.selectedRole || state.role || "unknown",
      segment_index: message.segmentIndex,
      http_status: Number.isInteger(message.httpStatus) ? message.httpStatus : null,
      error_category: errorCategory
    });
    const nextState = await saveState({
      ...state,
      backendConnected: message.backendUnavailable ? false : state.backendConnected,
      backendStatus: message.backendUnavailable ? "Not connected" : state.backendStatus,
      backendError: message.backendUnavailable
        ? "The verification request could not reach the backend."
        : state.backendError,
      identityStatus: message.apiError
        ? `API error (HTTP ${message.httpStatus})`
        : "Inconclusive",
      latestVerification: {
        role: message.selectedRole || state.role,
        segmentIndex: message.segmentIndex,
        httpStatus: message.httpStatus ?? null,
        transportError: message.error,
        response: null,
        receivedAt: new Date().toISOString()
      },
      scanStatus: `Segment ${message.segmentIndex} was not verified; capture continues.`
    });
    await updateTabIndicator(
      state.tabId,
      message.apiError
        ? `Verification error: ${errorCategory}.`
        : `INCONCLUSIVE — segment ${message.segmentIndex} was not verified.`,
      state.captureActive,
      message.apiError ? "error" : "inconclusive"
    );
    return { ok: true, state: nextState };
  }

  if (message.type === "CAPTURE_ERROR") {
    console.info("[ConsensusGuard] capture_error", {
      selected_role: state.role || "unknown",
      error_category: "capture_error"
    });
    const nextState = await saveState({
      ...state,
      scanSessionActive: false,
      captureActive: false,
      scanStatus: `Capture stopped: ${message.error}`,
      identityStatus: state.latestVerification ? state.identityStatus : "Inconclusive"
    });
    await updateTabIndicator(
      state.tabId,
      `Capture error: ${message.error}`,
      false,
      "error"
    );
    await chrome.offscreen.closeDocument().catch(() => {});
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
    .catch(() => {});
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
    .catch(() => {});
});

chrome.runtime.onInstalled.addListener(async () => {
  const stored = await chrome.storage.local.get("consensusGuardState");
  if (!stored.consensusGuardState) {
    await chrome.storage.local.set({ consensusGuardState: DEFAULT_STATE });
  }
});
