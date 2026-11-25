const VERIFICATION_URL = "http://localhost:8000/api/verify/audio";
const VIDEO_VERIFICATION_URL = "http://localhost:8000/api/verify/video";
const SEGMENT_DURATION_MS = 10_000;
const VIDEO_CLIP_DURATION_MS = 3_000;
const MIN_SEGMENT_DURATION_MS = 8_000;
const UPLOAD_TIMEOUT_MS = 45_000;
const MAX_PENDING_SEGMENTS = 3;

let captureActive = false;
let capturedStream = null;
let audioContext = null;
let audioSource = null;
let currentRecorder = null;
let currentVideoRecorder = null;
let segmentTimer = null;
let videoTimer = null;
let currentUploadController = null;
let uploadQueue = [];
let uploadInProgress = false;
let selectedRole = null;
let segmentIndex = 0;
let recordingMimeType = null;
let videoMimeType = null;

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

function getVideoMimeType() {
  const supportedTypes = [
    "video/webm;codecs=vp8",
    "video/webm;codecs=vp9",
    "video/webm"
  ];
  return supportedTypes.find(type => MediaRecorder.isTypeSupported(type)) || null;
}

async function cleanupCapture() {
  captureActive = false;
  clearTimeout(segmentTimer);
  segmentTimer = null;
  clearTimeout(videoTimer);
  videoTimer = null;

  const recorder = currentRecorder;
  currentRecorder = null;
  if (recorder && recorder.state !== "inactive") {
    try {
      recorder.stop();
    } catch {
      // The recorder may already have stopped after a browser error.
    }
  }

  const vRecorder = currentVideoRecorder;
  currentVideoRecorder = null;
  if (vRecorder && vRecorder.state !== "inactive") {
    try {
      vRecorder.stop();
    } catch {
      // Ignore already-stopped video recorder.
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
    await audioContext.close().catch(() => { });
  }
  audioContext = null;
  selectedRole = null;
  recordingMimeType = null;
  videoMimeType = null;
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

async function uploadVideoClip(blob, index) {
  if (!blob || !blob.size || !selectedRole) {
    return;
  }

  const formData = new FormData();
  formData.append("role", selectedRole);
  formData.append(
    "file",
    blob,
    `consensusguard-frame-${index}.jpg`
  );

  try {
    const response = await fetch(VIDEO_VERIFICATION_URL, {
      method: "POST",
      body: formData
    });
    if (!response.ok) {
      await sendBackgroundEvent("VIDEO_VERIFICATION_RESPONSE", {
        segmentIndex: index,
        httpStatus: response.status,
        response: { verdict: "inconclusive" }
      });
      return;
    }
    const payload = await response.json();
    await sendBackgroundEvent("VIDEO_VERIFICATION_RESPONSE", {
      segmentIndex: index,
      httpStatus: response.status,
      response: payload
    });
  } catch (error) {
    console.warn(`ConsensusGuard video check skipped: ${error.message}`);
    await sendBackgroundEvent("VIDEO_VERIFICATION_RESPONSE", {
      segmentIndex: index,
      httpStatus: 0,
      response: { verdict: "inconclusive" }
    });
  }
}

async function captureVideoSample(index) {
  if (!captureActive || !capturedStream) {
    return;
  }
  const videoTracks = capturedStream.getVideoTracks();
  if (!videoTracks.length) {
    await sendBackgroundEvent("VIDEO_VERIFICATION_RESPONSE", {
      segmentIndex: index,
      httpStatus: 0,
      response: { verdict: "inconclusive" }
    });
    return;
  }

  try {
    const videoEl = document.createElement("video");
    videoEl.muted = true;
    videoEl.playsInline = true;
    videoEl.srcObject = new MediaStream(videoTracks);
    await videoEl.play();

    // Wait 500ms so the first decoded video frame is ready
    await new Promise(resolve => setTimeout(resolve, 500));

    const width = videoEl.videoWidth || 1280;
    const height = videoEl.videoHeight || 720;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(videoEl, 0, 0, width, height);

    videoEl.pause();
    videoEl.srcObject = null;

    canvas.toBlob(blob => {
      if (captureActive && blob && blob.size > 0) {
        void uploadVideoClip(blob, index);
      }
    }, "image/jpeg", 0.92);
  } catch (error) {
    console.warn(`ConsensusGuard frame capture warning: ${error.message}`);
  }
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
    const audioOnlyStream = new MediaStream(capturedStream.getAudioTracks());
    recorder = new MediaRecorder(audioOnlyStream, {
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
    captureVideoSample(thisSegment);
    void sendBackgroundEvent("CAPTURE_PROGRESS", {
      status: `Recording 10-second audio + video segment ${thisSegment} for ${selectedRole}.`
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
    videoMimeType = getVideoMimeType();
    selectedRole = role;

    try {
      capturedStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          mandatory: {
            chromeMediaSource: "tab",
            chromeMediaSourceId: streamId
          }
        },
        video: {
          mandatory: {
            chromeMediaSource: "tab",
            chromeMediaSourceId: streamId
          }
        }
      });
    } catch {
      capturedStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          mandatory: {
            chromeMediaSource: "tab",
            chromeMediaSourceId: streamId
          }
        },
        video: false
      });
    }

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