const elements = {
  role: document.getElementById("speaker-role"),
  backendStatus: document.getElementById("backend-status"),
  backendError: document.getElementById("backend-error"),
  retryConnection: document.getElementById("retry-connection"),
  startScan: document.getElementById("start-scan"),
  stopScan: document.getElementById("stop-scan"),
  downloadAudit: document.getElementById("download-audit"),
  captureIndicator: document.getElementById("capture-indicator"),
  scanStatus: document.getElementById("scan-status"),
  actionError: document.getElementById("action-error"),
  identityStatus: document.getElementById("identity-status"),
  authenticityStatus: document.getElementById("authenticity-status"),
  videoStatus: document.getElementById("video-status"),
  quorumStatus: document.getElementById("quorum-status"),
  latestResult: document.getElementById("latest-result"),
  latestRole: document.getElementById("latest-role"),
  latestOutcome: document.getElementById("latest-outcome"),
  latestSimilarity: document.getElementById("latest-similarity"),
  latestAuthenticity: document.getElementById("latest-authenticity"),
  latestRequest: document.getElementById("latest-request"),
  latestTransportError: document.getElementById("latest-transport-error"),
  latestResponse: document.getElementById("latest-response")
};

let currentState = {
  backendConnected: false,
  backendStatus: "Not connected",
  scanSessionActive: false,
  scanStatus: "Not started",
  captureActive: false,
  tabId: null,
  identityStatus: "Not started",
  authenticityStatus: "Not started",
  videoAnalysisStatus: "Not started",
  quorumStatus: "Not started",
  latestVerification: null
};

async function sendBackgroundMessage(type, payload = {}) {
  return chrome.runtime.sendMessage({
    target: "background",
    type,
    ...payload
  });
}

function renderState(state) {
  currentState = { ...currentState, ...state };
  elements.backendStatus.textContent = currentState.backendStatus;
  elements.backendError.textContent = currentState.backendError || "";
  elements.backendError.hidden = !currentState.backendError;
  elements.scanStatus.textContent = currentState.scanStatus;

  elements.captureIndicator.textContent = currentState.captureActive
    ? "Capture active"
    : "Capture inactive";
  elements.captureIndicator.dataset.active = String(currentState.captureActive);

  if (currentState.scanSessionActive && currentState.role) {
    elements.role.value = currentState.role;
  }
  elements.role.disabled = currentState.scanSessionActive;
  elements.identityStatus.textContent = currentState.identityStatus;
  elements.authenticityStatus.textContent = currentState.authenticityStatus;
  elements.videoStatus.textContent = currentState.videoAnalysisStatus;
  elements.quorumStatus.textContent = currentState.quorumStatus;
  renderLatestVerification(currentState.latestVerification);

  elements.startScan.disabled = !currentState.backendConnected
    || !elements.role.value
    || currentState.scanSessionActive;
  elements.stopScan.disabled = !currentState.scanSessionActive;
}

function renderLatestVerification(latest) {
  elements.latestResult.hidden = !latest;
  if (!latest) {
    return;
  }

  const response = latest.response;
  elements.latestRole.textContent = `Selected participant: ${latest.role || "Unknown"}`;
  elements.latestTransportError.hidden = !latest.transportError;
  elements.latestTransportError.textContent = latest.transportError || "";

  if (!response) {
    const hasHttpStatus = Number.isInteger(latest.httpStatus);
    elements.latestOutcome.textContent = hasHttpStatus
      ? `Identity decision: API error (HTTP ${latest.httpStatus}; invalid response)`
      : "Identity decision: Inconclusive (no backend response)";
    elements.latestSimilarity.textContent = "Similarity: Not available";
    elements.latestAuthenticity.textContent = "Audio authenticity: Not available";
    elements.latestRequest.textContent = hasHttpStatus
      ? `Segment ${latest.segmentIndex}; HTTP ${latest.httpStatus}; no request ID returned`
      : `Segment ${latest.segmentIndex}; no request ID returned`;
    elements.latestResponse.textContent = hasHttpStatus
      ? "The backend returned a non-JSON response."
      : "No backend response was received.";
    return;
  }

  const decision = response.identity_status;
  const successfulResponse = latest.httpStatus >= 200 && latest.httpStatus < 300;
  const invalidAudio = Array.isArray(response.errors) && response.errors.some(
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
  const outcome = !successfulResponse
    ? invalidAudio
      ? `Invalid audio (HTTP ${latest.httpStatus})`
      : `API error (HTTP ${latest.httpStatus})`
    : decision === "match"
      ? "Match"
      : decision === "no_match"
        ? "No match"
        : decision === "error"
          ? "Verification error"
          : "Inconclusive";
  const similarity = typeof response.similarity === "number"
    ? String(response.similarity)
    : "Not provided";
  const authenticity = response.audio_authenticity_status || "Not provided";
  const requestId = response.request_id || "Not provided";
  const operationId = response.operation_id || "Not provided";

  elements.latestOutcome.textContent = `Backend identity decision: ${outcome} (${decision || "not provided"})`;
  elements.latestSimilarity.textContent = `Similarity: ${similarity}`;
  elements.latestAuthenticity.textContent = `Audio authenticity: ${authenticity}`;
  elements.latestRequest.textContent =
    `Segment ${latest.segmentIndex}; request ID: ${requestId}; operation ID: ${operationId}; HTTP ${latest.httpStatus}`;
  elements.latestResponse.textContent = JSON.stringify(response, null, 2);
}

function showActionError(message = "") {
  elements.actionError.textContent = message;
  elements.actionError.hidden = !message;
}

async function refreshState() {
  try {
    const response = await sendBackgroundMessage("GET_STATE");
    if (response.ok) {
      renderState(response.state);
    }
  } catch (error) {
    showActionError(`Could not read extension state: ${error.message}`);
  }
}

async function checkBackend() {
  elements.retryConnection.disabled = true;
  showActionError();
  try {
    const response = await sendBackgroundMessage("CHECK_BACKEND");
    if (!response.ok) {
      showActionError(response.error || "Backend check failed.");
      return;
    }
    renderState(response.state);
  } catch (error) {
    showActionError(`Backend check failed: ${error.message}`);
  } finally {
    elements.retryConnection.disabled = false;
  }
}

async function startScan() {
  elements.startScan.disabled = true;
  showActionError();
  try {
    const response = await sendBackgroundMessage("START_SCAN", {
      role: elements.role.value
    });
    if (!response.ok) {
      showActionError(response.error || "Could not prepare the scan session.");
      return;
    }
    renderState(response.state);
  } catch (error) {
    showActionError(`Could not prepare the scan session: ${error.message}`);
  } finally {
    renderState(currentState);
  }
}

async function stopScan() {
  elements.stopScan.disabled = true;
  showActionError();
  try {
    const response = await sendBackgroundMessage("STOP_SCAN");
    if (!response.ok) {
      showActionError(response.error || "Could not stop the scan session.");
      return;
    }
    renderState(response.state);
  } catch (error) {
    showActionError(`Could not stop the scan session: ${error.message}`);
  } finally {
    renderState(currentState);
  }
}

function downloadAuditReport() {
  showActionError();
  const meetingId = `CG-TAB-${currentState.tabId || "LIVE"}`;
  const url = `http://localhost:8000/api/meeting/audit/${encodeURIComponent(meetingId)}/download`;
  chrome.tabs.create({ url });
}

elements.role.addEventListener("change", () => renderState(currentState));
elements.retryConnection.addEventListener("click", checkBackend);
elements.startScan.addEventListener("click", startScan);
elements.stopScan.addEventListener("click", stopScan);
elements.downloadAudit.addEventListener("click", downloadAuditReport);

chrome.runtime.onMessage.addListener(message => {
  if (message?.target === "popup" && message.type === "STATE_UPDATED") {
    renderState(message.state);
  }
});

document.addEventListener("DOMContentLoaded", async () => {
  await refreshState();
  await checkBackend();
});