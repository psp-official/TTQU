import os
import uuid
import subprocess
from datetime import datetime
from flask import Flask, request, send_file, jsonify, render_template, after_this_request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = "/tmp/tiktok_hq_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Secret API Key (Change via environment variable API_KEY on your server)
EXPECTED_API_KEY = os.environ.get("API_KEY", "")

def check_auth(req):
    if not EXPECTED_API_KEY:
        return True
    key = req.headers.get("X-API-Key") or req.form.get("api_key")
    return key == EXPECTED_API_KEY

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "TikTok HQ Optimizer & Bypass Server",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })

@app.route("/api/optimize", methods=["POST"])
def optimize_video():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized: Invalid API Key"}), 401

    if "video" not in request.files:
        return jsonify({"error": "No video file uploaded"}), 400

    video_file = request.files["video"]
    if not video_file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Options: 'fast' (Lossless stream copy + Metadata Spoof + Faststart)
    #          'hq'   (Full Re-encode: 1080x1920 Lanczos, Unsharp, CRF 18, 60fps, bt709)
    mode = request.form.get("mode", "fast")
    fps = request.form.get("fps", "60")
    crf = request.form.get("crf", "18")
    sharpen = request.form.get("sharpen", "true") == "true"

    job_id = str(uuid.uuid4())
    ext = os.path.splitext(video_file.filename)[1].lower() or ".mp4"
    in_path = os.path.join(UPLOAD_DIR, f"in_{job_id}{ext}")
    out_path = os.path.join(UPLOAD_DIR, f"out_{job_id}.mp4")

    video_file.save(in_path)

    unique_comment = f"tt_hq_{uuid.uuid4().hex}"
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if mode == "fast":
        # Fast Remux Mode: Instantaneous, 0% CPU overhead, preserves 100% original quality,
        # strips old tracking metadata, injects fresh hash & moves moov atom to front.
        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-c", "copy",
            "-map_metadata", "-1",
            "-metadata", f"comment={unique_comment}",
            "-metadata", f"creation_time={now_str}",
            "-movflags", "+faststart",
            out_path
        ]
    else:
        # Full HQ Re-encode Mode: TikTok Sweet-Spot Specs to prevent server-side crushing
        vf_chain = "scale=1080:1920:flags=lanczos:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
        if sharpen:
            vf_chain += ",unsharp=3:3:0.5:3:3:0.0"
        vf_chain += ",format=yuv420p"

        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vf", vf_chain,
            "-r", str(fps),
            "-c:v", "libx264",
            "-profile:v", "high",
            "-level:v", "4.1",
            "-preset", "superfast",
            "-crf", str(crf),
            "-maxrate", "14M",
            "-bufsize", "28M",
            "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-map_metadata", "-1",
            "-metadata", f"comment={unique_comment}",
            "-metadata", f"creation_time={now_str}",
            "-movflags", "+faststart",
            out_path
        ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        if os.path.exists(in_path):
            os.remove(in_path)
        return jsonify({
            "error": "FFmpeg processing failed",
            "details": e.stderr.decode("utf-8", errors="ignore")[-500:]
        }), 500

    @after_this_request
    def cleanup(response):
        try:
            if os.path.exists(in_path):
                os.remove(in_path)
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return response

    clean_name = os.path.splitext(video_file.filename)[0] + "_HQ.mp4"
    return send_file(
        out_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=clean_name
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
