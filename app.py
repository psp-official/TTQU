import os
import json
import uuid
import struct
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify, Response, send_file, after_this_request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = "/tmp/ttqu_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def probe_video_info(filepath):
    """
    Uses ffprobe to detect input video width, height, and frame rate (up to 120 FPS+).
    """
    info = {"width": 1080, "height": 1920, "fps": 60.0}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
            "-of", "json",
            filepath
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            if streams:
                st = streams[0]
                info["width"] = int(st.get("width") or 1080)
                info["height"] = int(st.get("height") or 1920)
                fps_str = st.get("avg_frame_rate") or st.get("r_frame_rate") or "60/1"
                if "/" in fps_str:
                    num, den = fps_str.split("/", 1)
                    if float(den) > 0:
                        info["fps"] = float(num) / float(den)
                else:
                    info["fps"] = float(fps_str)
    except Exception as e:
        print(f"[warn] ffprobe failed: {e}")
    return info


def apply_server_side_hq_bypass_atoms(filepath):
    """
    Server-Side High Quality Bypass (Supports 1080P, 2K, 4K, and up to 120 FPS):
    1. Scans MP4 'tkhd' atom and normalizes track header resolution to Official 1080p (1080x1920 or 1920x1080).
    2. Scans MP4 'avcC' atom and spoofs AVCLevelIndication to Level 4.1 (0x29) so 120 FPS (Level 4.2) videos
       pass TikTok's strict High@4.1 stream check without framerate downsampling.
    3. Appends a unique 24-byte 'free' atom with cryptographic random bytes so TikTok's hash check is bypassed.
    """
    try:
        with open(filepath, "rb") as f:
            data = bytearray(f.read())

        limit = len(data) - 92
        i = 0
        while i < limit:
            # 1. Patch 'tkhd' atom resolution to 1080x1920 (or 1920x1080)
            if data[i:i+4] == b"tkhd":
                version = data[i + 4]
                width_offset = (i + 4 + 84) if version == 1 else (i + 4 + 72)
                height_offset = width_offset + 4
                if height_offset + 4 <= len(data):
                    w_fixed = struct.unpack(">I", data[width_offset:width_offset+4])[0]
                    h_fixed = struct.unpack(">I", data[height_offset:height_offset+4])[0]
                    w = w_fixed >> 16
                    h = h_fixed >> 16
                    if 0 < w <= 7680 and 0 < h <= 7680:
                        if h >= w:
                            struct.pack_into(">I", data, width_offset, 1080 << 16)
                            struct.pack_into(">I", data, height_offset, 1920 << 16)
                        else:
                            struct.pack_into(">I", data, width_offset, 1920 << 16)
                            struct.pack_into(">I", data, height_offset, 1080 << 16)
                i += 80
            # 2. Patch 'avcC' atom level to 4.1 (0x29 = 41) for 120 FPS bypass
            elif data[i:i+4] == b"avcC":
                # avcC layout: 'avcC' (4 bytes) + configurationVersion (1) + AVCProfileIndication (1) + profile_compatibility (1) + AVCLevelIndication (1)
                if i + 8 < len(data) and data[i + 4] == 1:
                    level_byte = data[i + 7]
                    if level_byte > 0x29:  # e.g. 0x2A (Level 4.2 for 120fps) or 0x33 (Level 5.1 for 4K)
                        data[i + 7] = 0x29
                i += 8
            else:
                i += 1

        # 3. Append unique 24-byte 'free' box
        random_seed = os.urandom(16)
        free_box = struct.pack(">I4s16s", 24, b"free", random_seed)
        data.extend(free_box)

        with open(filepath, "wb") as f:
            f.write(data)
    except Exception as e:
        print(f"[warn] apply_server_side_hq_bypass_atoms error: {e}")


# V7Patch script served to the Extension:
# 1. Disables Browser WebCodecs & AudioEncoder re-compression when High Quality Bypass is active
# 2. Locks user's selected Privacy (Only You / Friends / Everyone) & spoofs resolution/fps metadata in upload JSON
# 3. Normalizes MP4 'tkhd' (1080p) and 'avcC' (Level 4.1 for 120FPS) atoms & injects unique 'free' box
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
    if (window.AudioEncoder && window.AudioEncoder.isConfigSupported) {
      const _origAudioSupported = window.AudioEncoder.isConfigSupported.bind(window.AudioEncoder);
      window.AudioEncoder.isConfigSupported = async function(config) {
        if (window.__XUQING_BYPASS_ACTIVE__) {
          return { supported: false };
        }
        return _origAudioSupported(config);
      };
    }
  } catch (_) {}

  // 2. Privacy Guard + 1080P/2K/4K/120FPS Metadata Guard
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

    function patchUploadPayload(bodyStr) {
      try {
        if (typeof bodyStr !== 'string' || !bodyStr.trim().startsWith('{')) return bodyStr;
        let choice = window.__USER_PRIVACY_CHOICE__;
        let data = JSON.parse(bodyStr);
        let changed = false;

        function walk(obj) {
          if (!obj || typeof obj !== 'object') return;
          for (const k of Object.keys(obj)) {
            if (choice !== null && (k === 'visibility_type' || k === 'privacy_level' || k === 'private_status')) {
              obj[k] = choice;
              changed = true;
            } else if (window.__XUQING_BYPASS_ACTIVE__ && (k === 'original_resolution' || k === 'video_resolution')) {
              obj[k] = '1080p';
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
          init.body = patchUploadPayload(init.body);
        }
      } catch (_) {}
      return _origFetch.apply(this, arguments);
    };

    const _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function(body) {
      try {
        if (typeof body === 'string') {
          body = patchUploadPayload(body);
        }
      } catch (_) {}
      return _origSend.call(this, body);
    };
  }

  // Helper: Patch MP4 'tkhd' atom (1080p) and 'avcC' atom (Level 4.1 for 120FPS bypass)
  function patchMp4AtomsFor1080pAnd120Fps(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const limit = bytes.byteLength - 92;
    for (let i = 0; i < limit; i++) {
      // 'tkhd'
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
      // 'avcC' -> spoof AVCLevelIndication to 0x29 (Level 4.1) for 120FPS / 4K streams
      else if (bytes[i] === 0x61 && bytes[i+1] === 0x76 && bytes[i+2] === 0x63 && bytes[i+3] === 0x43) {
        if (i + 8 < bytes.byteLength && bytes[i + 4] === 1) {
          if (bytes[i + 7] > 0x29) {
            bytes[i + 7] = 0x29;
          }
        }
      }
    }
  }

  // 3. V7Patch Function: Byte-level MP4 1080P/2K/4K/120FPS Header Normalizer + Unique Box Injector
  window.V7Patch = async function V7Patch(file) {
    const ab = await file.arrayBuffer();
    if (!window.__XUQING_BYPASS_ACTIVE__) {
      return ab;
    }
    const bytes = new Uint8Array(ab);

    try {
      patchMp4AtomsFor1080pAnd120Fps(bytes);
    } catch (_) {}

    // If server already appended 'free' box at the end, return directly
    if (bytes.length >= 24 &&
        bytes[bytes.length - 20] === 0x66 &&
        bytes[bytes.length - 19] === 0x72 &&
        bytes[bytes.length - 18] === 0x65 &&
        bytes[bytes.length - 17] === 0x65) {
      return bytes.buffer;
    }

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

      const dt = new DataTransfer();
      dt.setData('text/plain', textToInsert);
      const pasteEvent = new ClipboardEvent('paste', {
        clipboardData: dt,
        bubbles: true,
        cancelable: true
      });
      editor.dispatchEvent(pasteEvent);

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
        "server": "xuqingxinnocent Self-Hosted 1080P/2K/4K/120FPS HQ Bypass Server",
        "supported_specs": ["1080P", "2K (1440P)", "4K (2160P)", "60FPS", "90FPS", "120FPS"],
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
        "info_message": "XUQINGXINNOCENT 1080P/2K/4K/120FPS ACTIVE",
        "update_message": "Connected to xuqingxinnocent 120FPS HQ server."
    })

@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    return jsonify({
        "enableV7Patch": True,
        "blocked": False,
        "maxFps": 120,
        "resolutions": ["1080p", "2k", "4k"]
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

    # Probe input resolution (1080P / 2K / 4K) and frame rate (30 / 60 / 90 / 120 FPS)
    vinfo = probe_video_info(in_path)
    in_fps = vinfo["fps"]
    is_high_fps = in_fps > 60.5

    # Build video filter:
    # - Lanczos scaling from 1080P/2K/4K to exact 1080p with Luma Unsharp filter
    # - Preserve native frame rate up to 120 FPS (if >120 FPS, cap cleanly at 120 FPS)
    vf_chain = "scale='if(gt(iw,ih),1920,1080)':-2:flags=lanczos,unsharp=3:3:0.35:3:3:0.0,format=yuv420p"
    if in_fps > 120.5:
        vf_chain += ",fps=120"

    # Use Level 4.2 during encode if >60 FPS (up to 120 FPS), then spoof 'avcC' atom to Level 4.1 (0x29) afterward!
    h264_level = "4.2" if is_high_fps else "4.1"
    gop_size = "120" if is_high_fps else "60"
    max_bitrate = "14M" if is_high_fps else "12M"
    buf_size = "28M" if is_high_fps else "24M"

    cmd = [
        "ffmpeg", "-y",
        "-i", in_path,
        "-vf", vf_chain,
        "-fps_mode", "passthrough",
        "-c:v", "libx264",
        "-preset", "faster",
        "-tune", "film",
        "-profile:v", "high",
        "-level:v", h264_level,
        "-crf", "17",
        "-maxrate", max_bitrate,
        "-bufsize", buf_size,
        "-g", gop_size,
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-c:a", "aac",
        "-b:a", "256k",
        "-ar", "48000",
        "-map_metadata", "-1",
        "-metadata", f"comment=xuqingxinnocent_120fps_hq_{job_id}",
        "-movflags", "+faststart",
        out_path
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Apply Server-Side Binary Atom Patch:
        # - 'tkhd' 1080p lock
        # - 'avcC' Level 4.1 (0x29) spoof so 120FPS passes TikTok's encoder check without downsampling
        # - unique 24-byte 'free' box
        apply_server_side_hq_bypass_atoms(out_path)
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

    return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output_1080p_120fps_xuqingxinnocent.mp4")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
