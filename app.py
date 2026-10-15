import os
import re
import json
import uuid
import urllib.request
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify, Response, send_file, after_this_request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = "/tmp/ttqu_uploads"
CONFIG_FILE = "/tmp/ttqu_remote_config.json"
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "xuqing123")

DEFAULT_REMOTE_CONFIG = {
    "blocked": os.environ.get("EXTENSION_BLOCKED", "false").lower() == "true",
    "update_required": os.environ.get("UPDATE_REQUIRED", "false").lower() == "true",
    "enableV7Patch": os.environ.get("ENABLE_V7_PATCH", "true").lower() != "false",
    "hqQualityModel": os.environ.get("HQ_QUALITY_MODEL", "true").lower() != "false",
    "min_version": os.environ.get("MIN_VERSION", "6.1.0"),
    "blacklisted_versions": [],
    "info_message": os.environ.get("INFO_MESSAGE", "UPDATE REQUIRED"),
    "update_message": os.environ.get(
        "UPDATE_MESSAGE",
        "New version is available or maintenance is in progress. Please update your extension."
    ),
    "update_url": os.environ.get("UPDATE_URL", "https://www.tiktok.com/@xuqingxinnocent"),
    "tele": os.environ.get("UPDATE_BTN_TEXT", "Download Update"),
    "update_url_2": os.environ.get("UPDATE_URL_2", ""),
    "wa": os.environ.get("UPDATE_BTN_TEXT_2", "Contact Admin")
}


def load_remote_config():
    cfg = dict(DEFAULT_REMOTE_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception:
            pass
    return cfg


def save_remote_config(new_cfg):
    cfg = load_remote_config()
    cfg.update(new_cfg)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return cfg


def probe_video_info(filepath):
    """
    Uses ffprobe to detect input video codec, width, height, and frame rate.
    """
    info = {"codec_name": "h264", "width": 1080, "height": 1920, "fps": 60.0}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate,avg_frame_rate",
            "-of", "json",
            filepath
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            if streams:
                st = streams[0]
                info["codec_name"] = (st.get("codec_name") or "h264").lower()
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


V7_PATCH_SCRIPT = r"""
(function() {
  'use strict';

  window.__XUQING_BYPASS_ACTIVE__ = true;
  window.__XUQING_HQ_MODEL_ACTIVE__ = true;
  window.__XUQING_WM_ACTIVE__ = true;

  window.addEventListener('message', function(e) {
    if (!e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'FPS_SET_ACTIVE') {
      window.__XUQING_BYPASS_ACTIVE__ = !!e.data.value;
    } else if (e.data.type === 'HQ_MODEL_SET_ACTIVE') {
      window.__XUQING_HQ_MODEL_ACTIVE__ = !!e.data.value;
    } else if (e.data.type === 'WM_SET_ACTIVE') {
      window.__XUQING_WM_ACTIVE__ = !!e.data.value;
    }
  });

  // Pass through the standardized 1080p 60FPS mastered video buffer cleanly
  window.V7Patch = async function V7Patch(file) {
    return await file.arrayBuffer();
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

WATERMARK_SCRIPT = r"""
(function() {
  'use strict';
  if (window.__XUQING_WM_INSTALLED__) return;
  window.__XUQING_WM_INSTALLED__ = true;
})();
"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>xuqingxinnocent — Remote Mastering Control Panel</title>
  <style>
    body {
      background: #0a0a0b; color: #f5f5f5; font-family: system-ui, -apple-system, sans-serif;
      display: flex; justify-content: center; padding: 32px 16px; margin: 0;
    }
    .panel {
      width: 100%; max-width: 460px; background: #111214;
      border: 1px solid rgba(0, 255, 106, 0.2); border-radius: 18px; padding: 24px;
    }
    h2 { margin: 0 0 6px; font-size: 18px; color: #00FF6A; }
    p.sub { margin: 0 0 20px; font-size: 12px; color: #8b92a0; }
    .row { margin-bottom: 14px; }
    label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 6px; color: #cfd5e0; }
    input[type="text"], input[type="password"], textarea {
      width: 100%; box-sizing: border-box; background: #181a1f; border: 1px solid #2a2e37;
      color: #fff; border-radius: 10px; padding: 10px 12px; font-size: 13px;
    }
    .toggle-row {
      display: flex; align-items: center; justify-content: space-between;
      background: #181a1f; padding: 12px 14px; border-radius: 12px; margin-bottom: 10px;
      border: 1px solid #232730;
    }
    .btn {
      width: 100%; background: #00FF6A; color: #0a0a0b; border: none; border-radius: 12px;
      padding: 12px; font-size: 14px; font-weight: 700; cursor: pointer; margin-top: 10px;
    }
    .status { margin-top: 12px; font-size: 12px; text-align: center; color: #00FF6A; }
  </style>
</head>
<body>
  <div class="panel">
    <h2>XUQINGXINNOCENT ADMIN</h2>
    <p class="sub">1080p 60FPS HEVC (hvc1) High-Bitrate Mastering Server</p>

    <div class="row">
      <label>Admin Key (Default: xuqing123)</label>
      <input type="password" id="adminKey" value="xuqing123">
    </div>

    <div class="toggle-row">
      <span>🔴 Maintenance Mode Overlay</span>
      <input type="checkbox" id="blocked">
    </div>

    <div class="toggle-row">
      <span>⚡ Enable 1080p 60FPS Mastering</span>
      <input type="checkbox" id="enableV7Patch" checked>
    </div>

    <div class="toggle-row">
      <span>💎 Enable HEVC (hvc1) ~11.5 Mbps High-Bitrate Mode</span>
      <input type="checkbox" id="hqQualityModel" checked>
    </div>

    <div class="row">
      <label>Minimum Allowed Version (e.g. 6.1.0)</label>
      <input type="text" id="min_version" value="6.1.0">
    </div>

    <div class="row">
      <label>Badge Title (info_message)</label>
      <input type="text" id="info_message" value="UPDATE REQUIRED">
    </div>

    <div class="row">
      <label>Popup Message (update_message)</label>
      <textarea id="update_message" rows="3"></textarea>
    </div>

    <div class="row">
      <label>Primary Button URL (update_url)</label>
      <input type="text" id="update_url">
    </div>

    <div class="row">
      <label>Primary Button Label (tele)</label>
      <input type="text" id="tele" value="Download Update">
    </div>

    <button class="btn" onclick="saveConfig()">Save & Apply to All Users</button>
    <div class="status" id="statusMsg"></div>
  </div>

  <script>
    async function loadConfig() {
      const res = await fetch('/api/checkup');
      const data = await res.json();
      document.getElementById('blocked').checked = !!data.blocked;
      document.getElementById('enableV7Patch').checked = data.enableV7Patch !== false;
      document.getElementById('hqQualityModel').checked = data.hqQualityModel !== false;
      document.getElementById('min_version').value = data.min_version || '6.1.0';
      document.getElementById('info_message').value = data.info_message || 'UPDATE REQUIRED';
      document.getElementById('update_message').value = data.update_message || '';
      document.getElementById('update_url').value = data.update_url || '';
      document.getElementById('tele').value = data.tele || 'Download Update';
    }
    async function saveConfig() {
      const payload = {
        key: document.getElementById('adminKey').value,
        blocked: document.getElementById('blocked').checked,
        update_required: document.getElementById('blocked').checked,
        enableV7Patch: document.getElementById('enableV7Patch').checked,
        hqQualityModel: document.getElementById('hqQualityModel').checked,
        min_version: document.getElementById('min_version').value.trim(),
        info_message: document.getElementById('info_message').value.trim(),
        update_message: document.getElementById('update_message').value.trim(),
        update_url: document.getElementById('update_url').value.trim(),
        tele: document.getElementById('tele').value.trim()
      };
      const res = await fetch('/api/admin/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const out = await res.json();
      document.getElementById('statusMsg').textContent = out.ok ? '✓ Saved & Live!' : ('Error: ' + out.error);
    }
    loadConfig();
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    cfg = load_remote_config()
    return jsonify({
        "status": "online",
        "server": "xuqingxinnocent 4K->1080p60 HEVC (hvc1) High-Bitrate Mastering Server",
        "supported_specs": ["1080x1920 @ 60FPS", "HEVC (hvc1) ~11.5 Mbps", "4K Lanczos Supersampling"],
        "admin_panel": "/admin",
        "extension_blocked": cfg.get("blocked", False),
        "mastering_enabled": cfg.get("enableV7Patch", True),
        "hevc_high_bitrate_mode": cfg.get("hqQualityModel", True),
        "min_version": cfg.get("min_version", "6.1.0"),
        "endpoints": ["/api/checkup", "/api/control", "/api/patch", "/api/inject", "/api/sign", "/api/transcode", "/admin"],
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })


@app.route("/admin", methods=["GET"])
def admin_panel():
    return Response(ADMIN_HTML, mimetype="text/html")


@app.route("/api/admin/config", methods=["POST"])
def api_admin_config():
    body = request.get_json(silent=True) or {}
    if body.get("key") != ADMIN_KEY:
        return jsonify({"ok": False, "error": "Invalid Admin Key"}), 403

    updates = {}
    for k in ["blocked", "update_required", "enableV7Patch", "hqQualityModel", "min_version", "info_message", "update_message", "update_url", "tele", "update_url_2", "wa"]:
        if k in body:
            updates[k] = body[k]

    cfg = save_remote_config(updates)
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/checkup", methods=["GET", "POST"])
def api_checkup():
    cfg = load_remote_config()
    return jsonify(cfg)


@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    cfg = load_remote_config()
    return jsonify({
        "enableV7Patch": cfg.get("enableV7Patch", True),
        "hqQualityModel": cfg.get("hqQualityModel", True),
        "blocked": cfg.get("blocked", False),
        "maxFps": 60,
        "resolutions": ["1080p"]
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
    cfg = load_remote_config()
    if cfg.get("blocked") or not cfg.get("enableV7Patch", True):
        return jsonify({"error": "Mastering is currently disabled by administrator."}), 403

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    # HQ Quality Mode (Default ON): 4K Downscale Mastering -> 1080x1920 @ 60FPS HEVC (hvc1) + High Bitrate (~11.5 Mbps)
    client_hq = request.args.get("hq_model", request.form.get("hq_model", "1")) == "1"
    use_hevc_hq = client_hq and cfg.get("hqQualityModel", True)

    video_file = request.files["video"]
    job_id = uuid.uuid4().hex
    in_path = os.path.join(UPLOAD_DIR, f"in_{job_id}.mp4")
    out_path = os.path.join(UPLOAD_DIR, f"out_{job_id}.mp4")
    video_file.save(in_path)

    vinfo = probe_video_info(in_path)
    in_w = vinfo["width"]
    in_h = vinfo["height"]
    in_fps = vinfo["fps"]
    in_codec = vinfo["codec_name"]

    # Supersampling Lanczos + Unsharp filter for 4K/2K -> 1080p (1080x1920 portrait or 1920x1080 landscape) @ 60 FPS
    vf_chain = (
        "scale='if(gt(iw,ih),1920,1080)':'if(gt(iw,ih),-2,1920)'"
        ":flags=lanczos+accurate_rnd+full_chroma_int,"
        "unsharp=5:5:0.55:5:5:0.0,fps=60,format=yuv420p"
    )

    # If the input video is ALREADY 1080p HEVC @ 60FPS, stream copy directly for instant 0-loss response
    if use_hevc_hq and in_codec in ("hevc", "h265") and (
        (in_w == 1080 and in_h == 1920) or (in_w == 1920 and in_h == 1080)
    ) and 58.0 <= in_fps <= 61.0:
        cmd_hevc = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-c:v", "copy",
            "-tag:v", "hvc1",
            "-c:a", "aac",
            "-b:a", "320k",
            "-ar", "48000",
            "-movflags", "+faststart",
            out_path
        ]
    else:
        # 4K Downscale Mastering: HEVC (hvc1) + High Bitrate (~11.5 Mbps target, maxrate 25 Mbps, bufsize 30 Mbps)
        cmd_hevc = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vf", vf_chain,
            "-vsync", "cfr",
            "-r", "60",
            "-c:v", "libx265",
            "-tag:v", "hvc1",
            "-preset", "faster",
            "-crf", "17",
            "-b:v", "11500k",
            "-maxrate", "25000k",
            "-bufsize", "30000k",
            "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
            "-c:a", "aac",
            "-b:a", "320k",
            "-ar", "48000",
            "-movflags", "+faststart",
            out_path
        ]

    # H.264 1080p 60FPS High-Bitrate Mastering (used when HQ Mode is OFF or as fallback)
    cmd_h264 = [
        "ffmpeg", "-y",
        "-i", in_path,
        "-vf", vf_chain,
        "-vsync", "cfr",
        "-r", "60",
        "-c:v", "libx264",
        "-preset", "superfast",
        "-tune", "film",
        "-profile:v", "high",
        "-level:v", "4.2",
        "-crf", "16",
        "-b:v", "11500k",
        "-maxrate", "25000k",
        "-bufsize", "30000k",
        "-g", "120",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-bf", "2",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
        "-c:a", "aac",
        "-b:a", "320k",
        "-ar", "48000",
        "-movflags", "+faststart",
        out_path
    ]

    try:
        if use_hevc_hq:
            try:
                subprocess.run(cmd_hevc, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError:
                subprocess.run(cmd_h264, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            subprocess.run(cmd_h264, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        if os.path.exists(in_path):
            os.remove(in_path)
        return jsonify({
            "error": "FFmpeg transcode failed",
            "details": e.stderr.decode("utf-8", errors="ignore")[-400:]
        }), 500

    @after_this_request
    def cleanup(response):
        try:
            if os.path.exists(in_path): os.remove(in_path)
            if os.path.exists(out_path): os.remove(out_path)
        except Exception:
            pass
        return response

    return send_file(
        out_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name="output_1080p60_master.mp4"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
