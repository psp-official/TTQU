import os
import json
import uuid
import logging
import subprocess
from dataclasses import dataclass
from typing import List, Dict
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response, send_file, after_this_request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Logging & App Initialization
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("HQVideoEngine")

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/ttqu_uploads")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/tmp/ttqu_remote_config.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "xuqing123")

DEFAULT_REMOTE_CONFIG = {
    "blocked": os.environ.get("EXTENSION_BLOCKED", "false").lower() == "true",
    "update_required": os.environ.get("UPDATE_REQUIRED", "false").lower() == "true",
    "hqQualityModel": os.environ.get("HQ_QUALITY_MODEL", "true").lower() != "false",
    "allowStreamCopy": os.environ.get("ALLOW_STREAM_COPY", "true").lower() != "false",
    "min_version": os.environ.get("MIN_VERSION", "6.1.0"),
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


def load_remote_config() -> dict:
    cfg = dict(DEFAULT_REMOTE_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception as exc:
            logger.warning(f"Failed to read remote config: {exc}")
    return cfg


def save_remote_config(new_cfg: dict) -> dict:
    cfg = load_remote_config()
    cfg.update(new_cfg)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"Failed to save remote config: {exc}")
    return cfg


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------
@dataclass
class VideoMetadata:
    codec_name: str
    pix_fmt: str
    width: int
    height: int
    fps: float
    bit_rate: int
    audio_codec: str

    @property
    def max_dimension(self) -> int:
        return max(self.width, self.height)

    @property
    def min_dimension(self) -> int:
        return min(self.width, self.height)

    @property
    def is_4k(self) -> bool:
        return self.max_dimension >= 3840 or self.min_dimension >= 2160

    @property
    def is_2k(self) -> bool:
        return not self.is_4k and (self.max_dimension >= 2560 or self.min_dimension >= 1440)

    @property
    def is_high_fps(self) -> bool:
        return self.fps > 60.5

    @property
    def tier_label(self) -> str:
        res = "4K" if self.is_4k else ("2K" if self.is_2k else "1080P")
        fps_label = "120FPS" if self.fps >= 100 else ("60FPS" if self.fps >= 50 else f"{int(round(self.fps))}FPS")
        return f"{res}_{fps_label}"


# ---------------------------------------------------------------------------
# Media Probing Service (ffprobe)
# ---------------------------------------------------------------------------
def probe_media(filepath: str) -> VideoMetadata:
    """
    Inspects both video and audio streams using ffprobe and returns structured metadata.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,bit_rate",
        "-of", "json",
        filepath
    ]

    codec_name = "unknown"
    pix_fmt = "yuv420p"
    width, height = 1080, 1920
    fps = 60.0
    bit_rate = 0
    audio_codec = "aac"

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=True
        )
        payload = json.loads(proc.stdout or "{}")
        streams = payload.get("streams", [])

        for st in streams:
            ctype = st.get("codec_type")
            if ctype == "video" and codec_name == "unknown":
                codec_name = (st.get("codec_name") or "h264").lower()
                pix_fmt = (st.get("pix_fmt") or "yuv420p").lower()
                width = int(st.get("width") or 1080)
                height = int(st.get("height") or 1920)
                bit_rate = int(st.get("bit_rate") or 0)

                fps_raw = st.get("avg_frame_rate") or st.get("r_frame_rate") or "60/1"
                if "/" in fps_raw:
                    num, den = fps_raw.split("/", 1)
                    if float(den) > 0:
                        fps = float(num) / float(den)
                else:
                    fps = float(fps_raw)

            elif ctype == "audio":
                audio_codec = (st.get("codec_name") or "aac").lower()

    except Exception as exc:
        logger.warning(f"ffprobe fallback triggered: {exc}")

    return VideoMetadata(
        codec_name=codec_name,
        pix_fmt=pix_fmt,
        width=width,
        height=height,
        fps=round(fps, 3),
        bit_rate=bit_rate,
        audio_codec=audio_codec
    )


# ---------------------------------------------------------------------------
# Encoding Profile Builder (Full Support for 1080p, 2K, 4K, 60FPS, 120FPS)
# ---------------------------------------------------------------------------
def select_h264_encoding_profile(meta: VideoMetadata) -> Dict[str, str]:
    """
    Selects the optimal H.264 level, CRF, VBV maxrate, and bufsize based on
    resolution (1080p / 2K / 4K) and frame rate (60FPS / 120FPS).
    """
    if meta.is_4k:
        if meta.is_high_fps:
            # 4K @ 120FPS requires High@L6.0
            return {"level": "6.0", "crf": "15", "maxrate": "65M", "bufsize": "130M", "gop": "120"}
        # 4K @ 60FPS requires High@L5.2
        return {"level": "5.2", "crf": "15", "maxrate": "45M", "bufsize": "90M", "gop": "60"}

    if meta.is_2k:
        if meta.is_high_fps:
            # 2K (1440p) @ 120FPS requires High@L5.2
            return {"level": "5.2", "crf": "15", "maxrate": "35M", "bufsize": "70M", "gop": "120"}
        # 2K (1440p) @ 60FPS requires High@L5.1
        return {"level": "5.1", "crf": "15", "maxrate": "25M", "bufsize": "50M", "gop": "60"}

    # 1080p Full HD
    if meta.is_high_fps:
        # 1080p @ 120FPS requires High@L5.1
        return {"level": "5.1", "crf": "16", "maxrate": "20M", "bufsize": "40M", "gop": "120"}

    # 1080p @ 60FPS requires High@L4.2
    return {"level": "4.2", "crf": "16", "maxrate": "15M", "bufsize": "30M", "gop": "60"}


def build_ffmpeg_command(
    in_path: str,
    out_path: str,
    meta: VideoMetadata,
    preserve_original: bool = True,
    allow_stream_copy: bool = True
) -> List[str]:
    """
    Constructs a rock-solid FFmpeg command line that preserves 2K/4K/60FPS/120FPS quality.
    """
    # 1. Zero-Loss Stream Copy when the source is already compliant 8-bit H.264 + AAC
    if (
        preserve_original
        and allow_stream_copy
        and meta.codec_name == "h264"
        and meta.pix_fmt == "yuv420p"
        and meta.audio_codec == "aac"
    ):
        logger.info(f"Using lossless stream copy for {meta.tier_label} ({meta.width}x{meta.height} @ {meta.fps}fps)")
        return [
            "ffmpeg", "-y",
            "-i", in_path,
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            out_path
        ]

    # 2. High-Precision Re-encode (for HEVC/VP9/AV1, 10-bit inputs, or standard 1080p mode)
    profile = select_h264_encoding_profile(meta)
    vf_filters = []

    if not preserve_original:
        # High-precision Lanczos scaling + Luma/Chroma unsharp when 1080p target mode is selected
        if meta.width > meta.height and meta.width > 1920:
            vf_filters.append("scale=1920:-2:flags=lanczos+accurate_rnd+full_chroma_int")
        elif meta.height >= meta.width and meta.height > 1920:
            vf_filters.append("scale=1080:-2:flags=lanczos+accurate_rnd+full_chroma_int")
        vf_filters.append("unsharp=3:3:0.35:3:3:0.0")
    else:
        # Ensure dimensions are divisible by 2 for yuv420p without changing 2K/4K resolution
        if meta.width % 2 != 0 or meta.height % 2 != 0:
            vf_filters.append("scale='trunc(iw/2)*2':'trunc(ih/2)*2':flags=lanczos")

    # Cap FPS at 120 only if source exceeds 120 FPS; otherwise keep exact 60FPS / 120FPS
    if meta.fps > 120.5:
        vf_filters.append("fps=120")

    vf_filters.append("format=yuv420p")
    vf_chain = ",".join(vf_filters)

    logger.info(
        f"Encoding {meta.tier_label} ({meta.width}x{meta.height} @ {meta.fps}fps) -> "
        f"H.264 High@L{profile['level']}, CRF={profile['crf']}, Maxrate={profile['maxrate']}"
    )

    return [
        "ffmpeg", "-y",
        "-i", in_path,
        "-vf", vf_chain,
        "-fps_mode", "passthrough",
        "-c:v", "libx264",
        "-preset", "faster",
        "-tune", "film",
        "-profile:v", "high",
        "-level:v", profile["level"],
        "-crf", profile["crf"],
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-g", profile["gop"],
        "-keyint_min", "30",
        "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-c:a", "aac",
        "-b:a", "320k",
        "-ar", "48000",
        "-movflags", "+faststart",
        out_path
    ]


# ---------------------------------------------------------------------------
# Admin Control Panel UI
# ---------------------------------------------------------------------------
ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HQ Video Engine — Admin Control Panel</title>
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
      border: 1px solid #232730; font-size: 13px;
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
    <h2>HQ VIDEO ENGINE ADMIN</h2>
    <p class="sub">2K / 4K / 60FPS / 120FPS Transcoding & Version Control</p>

    <div class="row">
      <label>Admin Key</label>
      <input type="password" id="adminKey" value="xuqing123">
    </div>

    <div class="toggle-row">
      <span>Maintenance / Force Update Lock</span>
      <input type="checkbox" id="blocked">
    </div>

    <div class="toggle-row">
      <span>Enable Original 2K/4K HQ Quality Mode</span>
      <input type="checkbox" id="hqQualityModel" checked>
    </div>

    <div class="toggle-row">
      <span>Enable Lossless Stream Copy (0% Loss)</span>
      <input type="checkbox" id="allowStreamCopy" checked>
    </div>

    <div class="row">
      <label>Minimum Allowed Version</label>
      <input type="text" id="min_version" value="6.1.0">
    </div>

    <div class="row">
      <label>Notice Title (info_message)</label>
      <input type="text" id="info_message" value="UPDATE REQUIRED">
    </div>

    <div class="row">
      <label>Notice Description (update_message)</label>
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

    <button class="btn" onclick="saveConfig()">Save Configuration</button>
    <div class="status" id="statusMsg"></div>
  </div>

  <script>
    async function loadConfig() {
      const res = await fetch('/api/checkup');
      const data = await res.json();
      document.getElementById('blocked').checked = !!data.blocked;
      document.getElementById('hqQualityModel').checked = data.hqQualityModel !== false;
      document.getElementById('allowStreamCopy').checked = data.allowStreamCopy !== false;
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
        hqQualityModel: document.getElementById('hqQualityModel').checked,
        allowStreamCopy: document.getElementById('allowStreamCopy').checked,
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
      document.getElementById('statusMsg').textContent = out.ok ? 'Saved & Live!' : ('Error: ' + out.error);
    }
    loadConfig();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health_check():
    cfg = load_remote_config()
    return jsonify({
        "status": "online",
        "service": "High-Quality 2K/4K 60FPS/120FPS Video Transcoding Engine",
        "supported_resolutions": ["1080P (FHD)", "2K (1440P QHD)", "4K (2160P UHD)"],
        "supported_framerates": ["30FPS", "60FPS", "90FPS", "120FPS"],
        "hq_quality_model": cfg.get("hqQualityModel", True),
        "allow_stream_copy": cfg.get("allowStreamCopy", True),
        "admin_panel": "/admin",
        "endpoints": ["/api/checkup", "/api/control", "/api/probe", "/api/transcode", "/admin"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/admin", methods=["GET"])
def admin_panel():
    return Response(ADMIN_HTML, mimetype="text/html")


@app.route("/api/admin/config", methods=["POST"])
def api_admin_config():
    body = request.get_json(silent=True) or {}
    if body.get("key") != ADMIN_KEY:
        return jsonify({"ok": False, "error": "Invalid Admin Key"}), 403

    allowed_keys = [
        "blocked", "update_required", "hqQualityModel", "allowStreamCopy",
        "min_version", "info_message", "update_message", "update_url",
        "tele", "update_url_2", "wa"
    ]
    updates = {k: body[k] for k in allowed_keys if k in body}
    cfg = save_remote_config(updates)
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/checkup", methods=["GET", "POST"])
def api_checkup():
    return jsonify(load_remote_config())


@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    cfg = load_remote_config()
    return jsonify({
        "hqQualityModel": cfg.get("hqQualityModel", True),
        "allowStreamCopy": cfg.get("allowStreamCopy", True),
        "blocked": cfg.get("blocked", False),
        "maxFps": 120,
        "resolutions": ["original", "1080p", "2k", "4k"]
    })


@app.route("/api/probe", methods=["POST"])
def api_probe():
    """
    Endpoint to inspect uploaded video resolution, FPS, and codec without transcoding.
    """
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    video_file = request.files["video"]
    job_id = uuid.uuid4().hex
    temp_path = os.path.join(UPLOAD_DIR, f"probe_{job_id}.mp4")
    video_file.save(temp_path)

    try:
        meta = probe_media(temp_path)
        return jsonify({
            "ok": True,
            "tier": meta.tier_label,
            "width": meta.width,
            "height": meta.height,
            "fps": meta.fps,
            "codec": meta.codec_name,
            "pix_fmt": meta.pix_fmt,
            "audio_codec": meta.audio_codec,
            "recommended_profile": select_h264_encoding_profile(meta)
        })
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.route("/api/transcode", methods=["POST"])
def api_transcode():
    """
    Main transcoding endpoint supporting:
    - hq_model=1 (default): Preserves 100% of 2K/4K/60FPS/120FPS resolution and frame rate.
    - stream_copy=1 (default): Uses lossless stream copy if input is already H.264 yuv420p.
    """
    cfg = load_remote_config()
    if cfg.get("blocked"):
        return jsonify({"error": "Service is currently under maintenance."}), 403

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    client_hq = request.args.get("hq_model", request.form.get("hq_model", "1")) == "1"
    preserve_original = client_hq and cfg.get("hqQualityModel", True)

    client_copy = request.args.get("stream_copy", request.form.get("stream_copy", "1")) == "1"
    allow_stream_copy = client_copy and cfg.get("allowStreamCopy", True)

    video_file = request.files["video"]
    job_id = uuid.uuid4().hex
    in_path = os.path.join(UPLOAD_DIR, f"in_{job_id}.mp4")
    out_path = os.path.join(UPLOAD_DIR, f"out_{job_id}.mp4")
    video_file.save(in_path)

    meta = probe_media(in_path)
    cmd = build_ffmpeg_command(
        in_path=in_path,
        out_path=out_path,
        meta=meta,
        preserve_original=preserve_original,
        allow_stream_copy=allow_stream_copy
    )

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    except subprocess.CalledProcessError as exc:
        err_tail = exc.stderr.decode("utf-8", errors="ignore")[-500:]
        logger.error(f"FFmpeg transcode failed for job {job_id}: {err_tail}")
        if os.path.exists(in_path):
            os.remove(in_path)
        return jsonify({"error": "FFmpeg transcode failed", "details": err_tail}), 500
    except subprocess.TimeoutExpired:
        if os.path.exists(in_path):
            os.remove(in_path)
        return jsonify({"error": "Transcode timed out"}), 504

    out_meta = probe_media(out_path)

    @after_this_request
    def cleanup(response):
        for path in (in_path, out_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        response.headers["X-Video-Tier"] = out_meta.tier_label
        response.headers["X-Video-Resolution"] = f"{out_meta.width}x{out_meta.height}"
        response.headers["X-Video-FPS"] = str(out_meta.fps)
        return response

    return send_file(
        out_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=f"hq_{out_meta.tier_label}.mp4"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
