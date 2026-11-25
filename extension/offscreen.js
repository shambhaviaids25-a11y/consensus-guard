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
let uploadQueue = [];
let uploadInProgress = false;
let selectedRole = null;
let segmentIndex = 0;
let recordingMimeType = null;

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

async function cleanupCapture() {
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
    currentUploadController.abort();
    currentUploadController = null;
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
}

async function failCapture(error) {
  if (!captureActive) {
    return;
  }
  await cleanupCapture();
  await sendBackgroundEvent("CAPTURE_ERROR", { error });
}

function enqueueSegment(blob, index) {
  if (!blob.size) {
    void sendBackgroundEvent("UPLOAD_FAILED", {
      segmentIndex: index,
      error: "The recorded segment was empty and was not submitted.",
      backendUnavailable: false
    });
    return;
  }

  if (uploadQueue.length >= MAX_PENDING_SEGMENTS) {
    void sendBackgroundEvent("UPLOAD_FAILED", {
      segmentIndex: index,
      error: "The upload queue is full; this segment was not submitted.",
      backendUnavailable: false
    });
    return;
  }

  uploadQueue.push({ blob, index });
  void drainUploadQueue();
}

async function uploadSegment(segment) {
  const controller = new AbortController();
  currentUploadController = controller;
  const timeoutId = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);
  const formData = new FormData();
  formData.append("role", selectedRole);
  formData.append(
    "audio",
    segment.blob,
    `consensusguard-segment-${segment.index}.webm`
  );
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
      await sendBackgroundEvent("UPLOAD_FAILED", {
        segmentIndex: segment.index,
        error: `Backend returned HTTP ${response.status} without a valid JSON response.`,
        backendUnavailable: false
      });
      return;
    }

    await sendBackgroundEvent("VERIFICATION_RESPONSE", {
      segmentIndex: segment.index,
      httpStatus: response.status,
      response: payload
    });
  } catch (error) {
    if (!captureActive && error.name === "AbortError") {
      return;
    }
    await sendBackgroundEvent("UPLOAD_FAILED", {
      segmentIndex: segment.index,
      error: error.name === "AbortError"
        ? "The verification upload timed out."
        : `Could not reach the verification backend: ${error.message}`,
      backendUnavailable: true
    });
  } finally {
    clearTimeout(timeoutId);
    if (currentUploadController === controller) {
      currentUploadController = null;
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
        status: `Uploading segment ${segment.index} for ${selectedRole}.`
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
  const startedAt = Date.now();
  let chunks = [];
  let recorder;

  try {
    recorder = new MediaRecorder(capturedStream, {
      mimeType: recordingMimeType
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

      const duration = Date.now() - startedAt;
      const blob = new Blob(chunks, { type: recordingMimeType });
      chunks = [];

      if (captureActive) {
        if (duration < MIN_SEGMENT_DURATION_MS) {
          void sendBackgroundEvent("UPLOAD_FAILED", {
            segmentIndex: thisSegment,
            error: "The audio segment was too short; no verification request was made.",
            backendUnavailable: false
          });
        } else {
          enqueueSegment(blob, thisSegment);
        }
        startRecordingSegment();
      }
    }, { once: true });

    recorder.start();
    void sendBackgroundEvent("CAPTURE_PROGRESS", {
      status: `Recording 10-second segment ${thisSegment} for ${selectedRole}.`
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

    captureActive = true;
    segmentIndex = 0;
    startRecordingSegment();
    return { ok: true, captureActive: true };
  } catch (error) {
    await cleanupCapture();
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
