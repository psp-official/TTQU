import os
import uuid
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify, Response, send_file, after_this_request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = "/tmp/ttqu_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# V7Patch script served to the Extension:
# 1. Disables Browser WebCodecs re-compression when High Quality Bypass is active
# 2. Locks user's selected Privacy (Only You / Friends / Everyone)
# 3. Spoofs 2K/4K MP4 Track Header ('tkhd') resolution atoms to Official 1080p (1080x1920) & injects unique 'free' box
V7_PATCH_SCRIPT = r"""
(function() {
  'use strict';

  window.__XUQING_BYPASS_ACTIVE__ = true;

  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'FPS_SET_ACTIVE') {
      window.__XUQING_BYPASS_ACTIVE__ = !!e.data.value;
    }
  });

  // 1. Disable Browser-Side WebCodecs Downscaling ONLY when High Quality Bypass is ON
  try {
    if (window.VideoEncoder && window.VideoEncoder.isConfigSupported) {
      const _origIsConfigSupported = window.VideoEncoder.isConfigSupported.bind(window.VideoEncoder);
      window.VideoEncoder.isConfigSupported = async function(config) {
        if (window.__XUQING_BYPASS_ACTIVE__) {
          return { supported: false };
        }
        return _origIsConfigSupported(config);
      };
    }
  } catch (_) {}

  // 2. Privacy Guard: Keep user's selected privacy (Only you / Friends / Everyone)
  if (!window.__TTQU_PRIVACY_GUARD__) {
    window.__TTQU_PRIVACY_GUARD__ = true;
    window.__USER_PRIVACY_CHOICE__ = null;

    function detectPrivacyFromText(txt) {
      if (!txt) return null;
      txt = txt.trim().toLowerCase();
      if (txt === 'only you' || txt === 'private' || txt === 'only me') return 2;
      if (txt === 'friends') return 1;
      if (txt === 'everyone' || txt === 'public') return 0;
      return null;
    }

    document.addEventListener('click', function(e) {
      try {
        const el = e.target.closest('[role="option"], [data-e2e*="privacy"], li, div, span');
        if (el) {
          const val = detectPrivacyFromText(el.innerText || el.textContent);
          if (val !== null) {
            window.__USER_PRIVACY_CHOICE__ = val;
          }
        }
      } catch (_) {}
    }, true);

    function patchPrivacyPayload(bodyStr) {
      try {
        if (typeof bodyStr !== 'string' || !bodyStr.trim().startsWith('{')) return bodyStr;
        let choice = window.__USER_PRIVACY_CHOICE__;
        if (choice === null) return bodyStr;

        let data = JSON.parse(bodyStr);
        let changed = false;

        function walk(obj) {
          if (!obj || typeof obj !== 'object') return;
          for (const k of Object.keys(obj)) {
            if (k === 'visibility_type' || k === 'privacy_level' || k === 'private_status') {
              obj[k] = choice;
              changed = true;
            } else if (typeof obj[k] === 'object') {
              walk(obj[k]);
            }
          }
        }
        walk(data);
        return changed ? JSON.stringify(data) : bodyStr;
      } catch (_) {
        return bodyStr;
      }
    }

    const _origFetch = window.fetch;
    window.fetch = async function(input, init) {
      try {
        if (init && typeof init.body === 'string') {
          init.body = patchPrivacyPayload(init.body);
        }
      } catch (_) {}
      return _origFetch.apply(this, arguments);
    };

    const _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function(body) {
      try {
        if (typeof body === 'string') {
          body = patchPrivacyPayload(body);
        }
      } catch (_) {}
      return _origSend.call(this, body);
    };
  }

  // Helper: Patch MP4 'tkhd' atom width/height to TikTok Official 1080p if > 1080p
  function patchMp4TkhdTo1080p(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const limit = bytes.byteLength - 92;
    for (let i = 0; i < limit; i++) {
      if (bytes[i] === 0x74 && bytes[i+1] === 0x6B && bytes[i+2] === 0x68 && bytes[i+3] === 0x64) {
        const version = bytes[i + 4];
        const widthOffset = version === 1 ? (i + 4 + 84) : (i + 4 + 72);
        const heightOffset = widthOffset + 4;
        if (heightOffset + 4 <= bytes.byteLength) {
          const wFixed = view.getUint32(widthOffset, false);
          const hFixed = view.getUint32(heightOffset, false);
          const w = wFixed >>> 16;
          const h = hFixed >>> 16;
          if ((w > 1080 && h > 1080) && w <= 7680 && h <= 7680) {
            if (h >= w) {
              view.setUint32(widthOffset, 1080 << 16, false);
              view.setUint32(heightOffset, 1920 << 16, false);
            } else {
              view.setUint32(widthOffset, 1920 << 16, false);
              view.setUint32(heightOffset, 1080 << 16, false);
            }
          }
        }
      }
    }
  }

  // 3. V7Patch Function: Byte-level MP4 1080p Header Normalizer + Unique Box Injector
  window.V7Patch = async function V7Patch(file) {
    const ab = await file.arrayBuffer();
    if (!window.__XUQING_BYPASS_ACTIVE__) {
      return ab;
    }
    const bytes = new Uint8Array(ab);

    try {
      patchMp4TkhdTo1080p(bytes);
    } catch (_) {}

    const randomSeed = new Uint8Array(16);
    crypto.getRandomValues(randomSeed);

    const freeBox = new Uint8Array(24);
    freeBox[0] = 0x00; freeBox[1] = 0x00; freeBox[2] = 0x00; freeBox[3] = 0x18;
    freeBox[4] = 0x66; freeBox[5] = 0x72; freeBox[6] = 0x65; freeBox[7] = 0x65; // 'free'
    freeBox.set(randomSeed, 8);

    const out = new Uint8Array(bytes.length + freeBox.length);
    out.set(bytes, 0);
    out.set(freeBox, bytes.length);

    return out.buffer;
  };
})();
"""

INJECT_SCRIPT = r"""
(function() {
  window.postMessage({ type: 'FPS_SHOW_BADGE' }, '*');
  window.addEventListener('message', function(e) {
    if (!e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'FPS_SHOW_BADGE' && typeof window.showBadge === 'function') {
      window.showBadge();
    } else if (e.data.type === 'FPS_HIDE_BADGE' && typeof window.hideBadge === 'function') {
      window.hideBadge();
    } else if (e.data.type === 'FPS_SET_ACTIVE' && typeof window._badge_update === 'function') {
      window._badge_update(e.data.value ? 'active' : 'off');
    }
  });
})();
"""

# Watermark script: Automatically adds "#xuqingxinnocent • Quality Uploaded ✓ @xuqingxinnocent"
# to the TikTok Studio caption editor when Watermark is turned ON!
WATERMARK_SCRIPT = r"""
(function() {
  'use strict';
  if (window.__XUQING_WM_INSTALLED__) return;
  window.__XUQING_WM_INSTALLED__ = true;

  const WM_TEXT = '#xuqingxinnocent \u2022 Quality Uploaded \u2713 @xuqingxinnocent ';
  const WM_MARKER = 'Quality Uploaded';
  let wmEnabled = true;
  let lastInsertedEditor = null;

  function findCaptionEditor() {
    const selectors = [
      '.public-DraftEditor-content[contenteditable="true"]',
      '[data-e2e="caption-container"] [contenteditable="true"]',
      '[class*="caption"] [contenteditable="true"]',
      '[class*="Caption"] [contenteditable="true"]',
      'div[contenteditable="true"][role="combobox"]',
      'div[contenteditable="true"]'
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }
    return null;
  }

  function insertWatermarkIntoEditor() {
    if (!wmEnabled) return false;
    const editor = findCaptionEditor();
    if (!editor) return false;

    const currentText = (editor.innerText || editor.textContent || '');
    if (currentText.includes(WM_MARKER) || currentText.includes('#xuqingxinnocent')) {
      return true;
    }

    try {
      editor.focus();

      // Clear default auto-filled filename if it ends with -xuqingxinnocent
      if (currentText.trim().endsWith('-xuqingxinnocent')) {
        document.execCommand('selectAll', false, null);
      } else {
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(editor);
        range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);
      }

      const prefix = (currentText.trim().length > 0 && !currentText.trim().endsWith('-xuqingxinnocent')) ? ' ' : '';
      const textToInsert = prefix + WM_TEXT;

      // Method 1: Simulated paste event (works best with TikTok Studio's Draft.js / Lexical editor)
      const dt = new DataTransfer();
      dt.setData('text/plain', textToInsert);
      const pasteEvent = new ClipboardEvent('paste', {
        clipboardData: dt,
        bubbles: true,
        cancelable: true
      });
      editor.dispatchEvent(pasteEvent);

      // Method 2: Fallback to execCommand('insertText') if paste was not handled by Draft.js
      setTimeout(() => {
        const afterText = (editor.innerText || editor.textContent || '');
        if (!afterText.includes(WM_MARKER)) {
          editor.focus();
          document.execCommand('insertText', false, textToInsert);
          editor.dispatchEvent(new InputEvent('input', {
            bubbles: true,
            cancelable: true,
            inputType: 'insertText',
            data: textToInsert
          }));
        }
      }, 80);

      lastInsertedEditor = editor;
      return true;
    } catch (_) {
      return false;
    }
  }

  function triggerWatermarkWithRetry() {
    if (!wmEnabled) return;
    let attempts = 0;
    const timer = setInterval(() => {
      attempts++;
      if (!wmEnabled || attempts > 30) {
        clearInterval(timer);
        return;
      }
      const ok = insertWatermarkIntoEditor();
      if (ok) {
        clearInterval(timer);
      }
    }, 500);
  }

  window.addEventListener('message', function(e) {
    if (!e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'WM_SET_ACTIVE') {
      wmEnabled = !!e.data.value;
      if (wmEnabled) {
        triggerWatermarkWithRetry();
      }
    } else if (e.data.type === 'WM_VIDEO_UPLOADED') {
      if (wmEnabled) {
        triggerWatermarkWithRetry();
      }
    }
  });

  // Also observe DOM for when TikTok Studio switches from file dropzone to the caption editor screen
  const observer = new MutationObserver(() => {
    if (!wmEnabled) return;
    const editor = findCaptionEditor();
    if (editor && editor !== lastInsertedEditor) {
      const txt = (editor.innerText || editor.textContent || '');
      if (!txt.includes(WM_MARKER)) {
        insertWatermarkIntoEditor();
      }
    }
  });

  if (document.documentElement) {
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }
})();
"""

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "server": "xuqingxinnocent Self-Hosted 2K-to-1080p HQ Server",
        "endpoints": ["/api/checkup", "/api/control", "/api/patch", "/api/inject", "/api/sign", "/api/transcode"],
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })

@app.route("/api/checkup", methods=["GET", "POST"])
def api_checkup():
    return jsonify({
        "blocked": False,
        "update_required": False,
        "min_version": "1.0.0",
        "blacklisted_versions": [],
        "info_message": "XUQINGXINNOCENT ACTIVE",
        "update_message": "Connected to xuqingxinnocent server."
    })

@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    return jsonify({
        "enableV7Patch": True,
        "blocked": False
    })

@app.route("/api/patch", methods=["GET", "POST"])
def api_patch():
    return Response(V7_PATCH_SCRIPT, mimetype="application/javascript")

@app.route("/api/inject", methods=["GET", "POST"])
def api_inject():
    return Response(INJECT_SCRIPT, mimetype="application/javascript")

@app.route("/api/sign", methods=["GET", "POST"])
def api_sign():
    return Response(WATERMARK_SCRIPT, mimetype="application/javascript")

@app.route("/api/transcode", methods=["POST"])
def api_transcode():
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    video_file = request.files["video"]
    job_id = uuid.uuid4().hex
    in_path = os.path.join(UPLOAD_DIR, f"in_{job_id}.mp4")
    out_path = os.path.join(UPLOAD_DIR, f"out_{job_id}.mp4")
    video_file.save(in_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", in_path,
        "-vf", "scale='if(gt(iw,ih),min(1920,iw),min(1080,iw))':-2:flags=lanczos,format=yuv420p",
        "-c:v", "libx264",
        "-preset", "superfast",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-crf", "18",
        "-maxrate", "14M",
        "-bufsize", "28M",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-c:a", "aac",
        "-b:a", "192k",
        "-map_metadata", "-1",
        "-metadata", f"comment=xuqingxinnocent_{job_id}",
        "-movflags", "+faststart",
        out_path
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        if os.path.exists(in_path):
            os.remove(in_path)
        return jsonify({"error": "FFmpeg transcode failed", "details": e.stderr.decode("utf-8", errors="ignore")[-400:]}), 500

    @after_this_request
    def cleanup(response):
        try:
            if os.path.exists(in_path): os.remove(in_path)
            if os.path.exists(out_path): os.remove(out_path)
        except Exception:
            pass
        return response

    return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output_1080p_xuqingxinnocent.mp4")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
