const VERIFICATION_URL = "http://localhost:8000/api/verify/audio";
const SEGMENT_DURATION_MS = 10_000;
const MIN_SEGMENT_DURATION_MS = 8_000;
const UPLOAD_TIMEOUT_MS = 45_000;
const MAX_PENDING_SEGMENTS = 3;

let captureActive = false;
let capturedStream = null;
let audioContext = null;
let audioSource = null;
let currentRecorder = null;
let segmentTimer = null;
let currentUploadController = null;
let currentUploadSegment = null;
let uploadQueue = [];
let uploadInProgress = false;
let selectedRole = null;
let segmentIndex = 0;
let recordingMimeType = null;

function logAudioDiagnostic(event, details) {
  console.info(`[ConsensusGuard] ${event}`, details);
}

function getAudioTrackDiagnostics(track) {
  const settings = typeof track.getSettings === "function"
    ? track.getSettings()
    : {};
  return {
    sample_rate_hz: Number.isFinite(settings.sampleRate) ? settings.sampleRate : null,
    channel_count: Number.isFinite(settings.channelCount) ? settings.channelCount : null,
    muted: Boolean(track.muted),
    ready_state: track.readyState
  };
}

function getResponseErrorCategory(httpStatus, payload) {
  const errors = Array.isArray(payload.errors) ? payload.errors : [];
  for (const entry of errors) {
    const code = entry?.error?.code;
    if (typeof code === "string" && /^[A-Za-z0-9_.:-]{1,64}$/.test(code)) {
      return code;
    }
  }
  if (httpStatus === 402) {
    return "payment_required";
  }
  if (httpStatus < 200 || httpStatus >= 300) {
    return `http_${httpStatus}`;
  }
  if (payload.identity_status === "error") {
    return "verification_error";
  }
  if (!["match", "no_match"].includes(payload.identity_status)) {
    return "missing_identity_decision";
  }
  return "none";
}

async function sendBackgroundEvent(type, details = {}) {
  try {
    return await chrome.runtime.sendMessage({
      target: "background",
      type,
      ...details
    });
  } catch (error) {
    console.error(`ConsensusGuard could not report ${type}: ${error.message}`);
    return null;
  }
}

function getRecordingMimeType() {
  const supportedTypes = [
    "audio/webm;codecs=opus",
    "audio/webm"
  ];
  const mimeType = supportedTypes.find(type => MediaRecorder.isTypeSupported(type));
  if (!mimeType) {
    throw new Error("This browser does not support WebM audio recording.");
  }
  return mimeType;
}

async function cleanupCapture(reason = "user_stop") {
  const hadCapture = captureActive || capturedStream !== null;
  const stoppedRole = selectedRole;
  captureActive = false;
  clearTimeout(segmentTimer);
  segmentTimer = null;

  const recorder = currentRecorder;
  currentRecorder = null;
  if (recorder && recorder.state !== "inactive") {
    try {
      recorder.stop();
    } catch {
      // The recorder may already have stopped after a browser error.
    }
  }

  if (currentUploadController) {
    if (currentUploadSegment) {
      logAudioDiagnostic("verification_upload_cancelled", {
        selected_role: currentUploadSegment.role || stoppedRole || "unknown",
        segment_index: currentUploadSegment.index,
        audio_mime_type: currentUploadSegment.mimeType || "unknown",
        duration_seconds: currentUploadSegment.durationSeconds,
        audio_bytes: currentUploadSegment.blob.size,
        audio_empty: currentUploadSegment.blob.size === 0,
        error_category: reason
      });
    }
    currentUploadController.abort();
    currentUploadController = null;
    currentUploadSegment = null;
  }
  uploadQueue = [];

  if (audioSource) {
    try {
      audioSource.disconnect();
    } catch {
      // The source may already be disconnected.
    }
    audioSource = null;
  }

  if (capturedStream) {
    for (const track of capturedStream.getTracks()) {
      track.onended = null;
      try {
        track.stop();
      } catch (error) {
        console.error(`ConsensusGuard could not stop a media track: ${error.message}`);
      }
    }
    capturedStream = null;
  }

  if (audioContext && audioContext.state !== "closed") {
    await audioContext.close().catch(() => {});
  }
  audioContext = null;
  selectedRole = null;
  recordingMimeType = null;
  if (hadCapture) {
    logAudioDiagnostic("capture_stopped", {
      selected_role: stoppedRole || "unknown",
      segment_index: segmentIndex,
      reason
    });
  }
}

async function failCapture(error) {
  if (!captureActive) {
    return;
  }
  await cleanupCapture("capture_error");
  await sendBackgroundEvent("CAPTURE_ERROR", { error });
}

function enqueueSegment(blob, index, durationSeconds, role, mimeType) {
  if (!blob.size) {
    logAudioDiagnostic("audio_segment_rejected", {
      selected_role: role || "unknown",
      segment_index: index,
      audio_mime_type: mimeType || "unknown",
      duration_seconds: durationSeconds,
      audio_bytes: blob.size,
      audio_empty: true,
      error_category: "empty_audio"
    });
    void sendBackgroundEvent("UPLOAD_FAILED", {
      segmentIndex: index,
      error: "The recorded segment was empty and was not submitted.",
      backendUnavailable: false,
      errorCategory: "empty_audio",
      selectedRole: role
    });
    return;
  }

  if (uploadQueue.length >= MAX_PENDING_SEGMENTS) {
    logAudioDiagnostic("audio_segment_rejected", {
      selected_role: role || "unknown",
      segment_index: index,
      audio_mime_type: mimeType || "unknown",
      duration_seconds: durationSeconds,
      audio_bytes: blob.size,
      audio_empty: false,
      error_category: "upload_queue_full"
    });
    void sendBackgroundEvent("UPLOAD_FAILED", {
      segmentIndex: index,
      error: "The upload queue is full; this segment was not submitted.",
      backendUnavailable: false,
      errorCategory: "upload_queue_full",
      selectedRole: role
    });
    return;
  }

  uploadQueue.push({
    blob,
    index,
    durationSeconds,
    role,
    mimeType
  });
  void drainUploadQueue();
}

async function uploadSegment(segment) {
  const controller = new AbortController();
  currentUploadController = controller;
  currentUploadSegment = segment;
  const timeoutId = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);
  const formData = new FormData();
  formData.append("role", segment.role);
  formData.append(
    "audio",
    segment.blob,
    `consensusguard-segment-${segment.index}.webm`
  );
  formData.append("capture_duration_seconds", String(segment.durationSeconds));
  formData.append("detect_deepfake", "false");

  try {
    const response = await fetch(VERIFICATION_URL, {
      method: "POST",
      body: formData,
      signal: controller.signal
    });
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }

    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      logAudioDiagnostic("verification_response", {
        selected_role: segment.role || "unknown",
        segment_index: segment.index,
        audio_mime_type: segment.mimeType || "unknown",
        duration_seconds: segment.durationSeconds,
        audio_bytes: segment.blob.size,
        audio_empty: segment.blob.size === 0,
        http_status: response.status,
        identity_status: null,
        similarity: null,
        error_category: "invalid_json_response"
      });
      await sendBackgroundEvent("UPLOAD_FAILED", {
        segmentIndex: segment.index,
        error: `Backend returned HTTP ${response.status} without a valid JSON response.`,
        backendUnavailable: false,
        apiError: true,
        httpStatus: response.status,
        errorCategory: "invalid_json_response",
        selectedRole: segment.role
      });
      return;
    }

    logAudioDiagnostic("verification_response", {
      selected_role: segment.role || "unknown",
      segment_index: segment.index,
      audio_mime_type: segment.mimeType || "unknown",
      duration_seconds: segment.durationSeconds,
      audio_bytes: segment.blob.size,
      audio_empty: segment.blob.size === 0,
      http_status: response.status,
      identity_status: typeof payload.identity_status === "string"
        ? payload.identity_status
        : null,
      similarity: Number.isFinite(payload.similarity) ? payload.similarity : null,
      error_category: getResponseErrorCategory(response.status, payload)
    });
    await sendBackgroundEvent("VERIFICATION_RESPONSE", {
      segmentIndex: segment.index,
      httpStatus: response.status,
      selectedRole: segment.role,
      audioMimeType: segment.mimeType,
      durationSeconds: segment.durationSeconds,
      audioBytes: segment.blob.size,
      audioEmpty: segment.blob.size === 0,
      response: payload
    });
  } catch (error) {
    if (!captureActive && error.name === "AbortError") {
      return;
    }
    const errorCategory = error.name === "AbortError"
      ? "upload_timeout"
      : "backend_unreachable";
    logAudioDiagnostic("verification_transport_error", {
      selected_role: segment.role || "unknown",
      segment_index: segment.index,
      audio_mime_type: segment.mimeType || "unknown",
      duration_seconds: segment.durationSeconds,
      audio_bytes: segment.blob.size,
      audio_empty: segment.blob.size === 0,
      error_category: errorCategory
    });
    await sendBackgroundEvent("UPLOAD_FAILED", {
      segmentIndex: segment.index,
      error: error.name === "AbortError"
        ? "The verification upload timed out."
        : `Could not reach the verification backend: ${error.message}`,
      backendUnavailable: true,
      errorCategory,
      selectedRole: segment.role
    });
  } finally {
    clearTimeout(timeoutId);
    if (currentUploadController === controller) {
      currentUploadController = null;
    }
    if (currentUploadSegment === segment) {
      currentUploadSegment = null;
    }
  }
}

async function drainUploadQueue() {
  if (uploadInProgress) {
    return;
  }

  uploadInProgress = true;
  try {
    while (captureActive && uploadQueue.length) {
      const segment = uploadQueue.shift();
      await sendBackgroundEvent("CAPTURE_PROGRESS", {
        status: `Uploading segment ${segment.index} for ${segment.role}.`
      });
      if (!captureActive) {
        break;
      }
      await uploadSegment(segment);
    }
  } finally {
    uploadInProgress = false;
    if (captureActive && uploadQueue.length) {
      void drainUploadQueue();
    }
  }
}

function startRecordingSegment() {
  if (!captureActive || !capturedStream) {
    return;
  }

  segmentIndex += 1;
  const thisSegment = segmentIndex;
  const segmentRole = selectedRole;
  const segmentMimeType = recordingMimeType;
  const startedAt = performance.now();
  let chunks = [];
  let recorder;

  try {
    recorder = new MediaRecorder(capturedStream, {
      mimeType: segmentMimeType
    });
    currentRecorder = recorder;
    recorder.addEventListener("dataavailable", event => {
      if (event.data && event.data.size) {
        chunks.push(event.data);
      }
    });
    recorder.addEventListener("error", () => {
      void failCapture("The browser could not record tab audio.");
    }, { once: true });
    recorder.addEventListener("stop", () => {
      clearTimeout(segmentTimer);
      segmentTimer = null;
      if (currentRecorder === recorder) {
        currentRecorder = null;
      }

      const durationSeconds = (performance.now() - startedAt) / 1000;
      const blob = new Blob(chunks, { type: segmentMimeType });
      const actualMimeType = blob.type || segmentMimeType;
      const chunkCount = chunks.length;
      chunks = [];

      logAudioDiagnostic("audio_segment_ready", {
        selected_role: segmentRole || "unknown",
        segment_index: thisSegment,
        audio_mime_type: actualMimeType || "unknown",
        duration_seconds: durationSeconds,
        audio_bytes: blob.size,
        audio_empty: blob.size === 0,
        chunks_count: chunkCount,
        recording_complete: true
      });

      if (captureActive) {
        if (durationSeconds * 1000 < MIN_SEGMENT_DURATION_MS) {
          logAudioDiagnostic("audio_segment_rejected", {
            selected_role: segmentRole || "unknown",
            segment_index: thisSegment,
            audio_mime_type: actualMimeType || "unknown",
            duration_seconds: durationSeconds,
            audio_bytes: blob.size,
            audio_empty: blob.size === 0,
            error_category: "segment_too_short"
          });
          void sendBackgroundEvent("UPLOAD_FAILED", {
            segmentIndex: thisSegment,
            error: "The audio segment was too short; no verification request was made.",
            backendUnavailable: false,
            errorCategory: "segment_too_short",
            selectedRole: segmentRole
          });
        } else {
          enqueueSegment(
            blob,
            thisSegment,
            durationSeconds,
            segmentRole,
            actualMimeType
          );
        }
        startRecordingSegment();
      }
    }, { once: true });

    recorder.start();
    if (thisSegment === 1) {
      logAudioDiagnostic("capture_started", {
        selected_role: selectedRole || "unknown",
        audio_mime_type: segmentMimeType,
        audio_context_sample_rate_hz: audioContext?.sampleRate || null,
        segment_duration_target_seconds: SEGMENT_DURATION_MS / 1000
      });
    } else {
      logAudioDiagnostic("recording_segment_started", {
        selected_role: selectedRole || "unknown",
        segment_index: thisSegment,
        audio_mime_type: segmentMimeType
      });
    }
    void sendBackgroundEvent("CAPTURE_PROGRESS", {
      status: `Recording 10-second segment ${thisSegment} for ${segmentRole}.`
    });
    segmentTimer = setTimeout(() => {
      if (captureActive && recorder.state === "recording") {
        recorder.stop();
      }
    }, SEGMENT_DURATION_MS);
  } catch (error) {
    void failCapture(`Could not record tab audio: ${error.message}`);
  }
}

async function startCapture(streamId, role) {
  if (captureActive) {
    return { ok: false, error: "A tab-audio capture session is already active." };
  }

  try {
    recordingMimeType = getRecordingMimeType();
    selectedRole = role;

    capturedStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId
        }
      },
      video: false
    });
    if (!capturedStream.getAudioTracks().length) {
      throw new Error("The selected tab did not provide an audio track.");
    }

    audioContext = new AudioContext();
    audioSource = audioContext.createMediaStreamSource(capturedStream);
    audioSource.connect(audioContext.destination);
    await audioContext.resume();
    if (audioContext.state !== "running") {
      throw new Error("The captured tab audio could not be routed to playback.");
    }

    for (const track of capturedStream.getAudioTracks()) {
      track.onended = () => {
        void failCapture("The meeting tab audio stream ended.");
      };
    }

    const audioTrack = capturedStream.getAudioTracks()[0];
    captureActive = true;
    segmentIndex = 0;
    logAudioDiagnostic("capture_input", {
      selected_role: selectedRole,
      ...getAudioTrackDiagnostics(audioTrack),
      audio_context_sample_rate_hz: audioContext.sampleRate
    });
    startRecordingSegment();
    return { ok: true, captureActive: true };
  } catch (error) {
    logAudioDiagnostic("capture_start_failed", {
      selected_role: selectedRole || "unknown",
      error_category: "capture_start_error"
    });
    await cleanupCapture("startup_error");
    return { ok: false, error: error.message };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== "offscreen") {
    return false;
  }

  if (message.type === "CAPTURE_START") {
    startCapture(message.streamId, message.role)
      .then(sendResponse)
      .catch(error => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message.type === "CAPTURE_STOP") {
    cleanupCapture()
      .then(() => sendResponse({ ok: true, captureActive: false }))
      .catch(error => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  return false;
});
