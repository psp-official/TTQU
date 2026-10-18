import os
import re
import json
import uuid
import struct
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

# Cached TikTok profile IDs for @xuqingxinnocent so mention is always clickable
MENTION_CACHE = {
    "unique_id": "xuqingxinnocent",
    "user_id": os.environ.get("XUQING_USER_ID", ""),
    "sec_uid": os.environ.get("XUQING_SEC_UID", "")
}


def resolve_tiktok_user_from_server(username="xuqingxinnocent"):
    if MENTION_CACHE["user_id"] and MENTION_CACHE["sec_uid"]:
        return MENTION_CACHE
    try:
        req = urllib.request.Request(
            f"https://www.tiktok.com/@{username}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"
            }
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            m_id = (
                re.search(r'"user":\{[^}]*"id":"(\d+)"[^}]*"uniqueId":"' + re.escape(username) + r'"', html, re.I)
                or re.search(r'"uniqueId":"' + re.escape(username) + r'"[^}]*"id":"(\d+)"', html, re.I)
                or re.search(r'"userInfo":\{"user":\{"id":"(\d+)"', html)
            )
            m_sec = re.search(r'"secUid":"(MS4wLjABAAAA[A-Za-z0-9_-]+)"', html)
            if m_id:
                MENTION_CACHE["user_id"] = m_id.group(1)
            if m_sec:
                MENTION_CACHE["sec_uid"] = m_sec.group(1)
    except Exception as e:
        print(f"[warn] resolve_tiktok_user_from_server failed: {e}")
    return MENTION_CACHE


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
    "wa": os.environ.get("UPDATE_BTN_TEXT_2", "Contact Admin"),
    "xuqing_user_id": os.environ.get("XUQING_USER_ID", ""),
    "xuqing_sec_uid": os.environ.get("XUQING_SEC_UID", "")
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
    Uses ffprobe to detect input video codec, width, height, and frame rate (up to 120 FPS+).
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


def build_bytedance_fast_import_udta(width=1080, height=1920, duration_ms=15000):
    """
    Constructs the exact Apple/QuickTime 'udta' -> 'meta' -> ['hdlr', 'keys', 'ilst'] atom
    used by TikTok / CapCut / Aweme (sourceAppID: 1233, te_is_fast_import: 1, Hw: 1).
    """
    import hashlib
    uid1 = hashlib.md5(f"aweme_{width}_{height}_{duration_ms}".encode()).hexdigest()
    uid2 = hashlib.md5(f"import_{duration_ms}_{width}".encode()).hexdigest()

    kv_pairs = [
        ("Hw", "1"),
        ("bitrate", "14874999"),
        ("com.apple.quicktime.information", json.dumps({
            "data": {
                "prop_list": [],
                "met_publish_data": [{
                    "effect_list": "",
                    "effect_source": "",
                    "is_parent_template_used": False,
                    "is_prop_panel_cache_list": "",
                    "met_parent_resource_ids": "",
                    "prop_category": "",
                    "prop_list": "",
                    "prop_panel_delivery_source": "",
                    "prop_panel_pin_type": "",
                    "prop_impr_position": "",
                    "prop_selected_from": "",
                    "resource_list": "",
                    "tab_key": ""
                }],
                "product": "aweme"
            }
        }, separators=(',', ':'))),
        ("copyright", uid1),
        ("description", json.dumps([{
            "creationDate": "2026-10-15T08:30:00+0630",
            "make": "",
            "videoIndex": 1,
            "userSystem": "14",
            "appRecord": "0",
            "MD5": uid2,
            "videoDevice": "",
            "system": "14",
            "userDevice": "22101316UCP",
            "deviceResolution": [],
            "importPath": "/data/user/0/com.zhiliaoapp.musically/files/CreativeTool/creative/export/0/external_save/" + uid1[:15],
            "videoResolution": {"width": width, "height": height},
            "videoDuration": duration_ms,
            "isRecord": 0,
            "isCropped": 1,
            "sourceAppID": 1233
        }], separators=(',', ':'))),
        ("maxrate", "30000000"),
        ("te_is_fast_import", "1"),
        ("te_is_reencode", "1"),
        ("encoder", "Lavf57.71.100")
    ]

    hdlr_atom = b"\x00\x00\x00!hdlr\x00\x00\x00\x00\x00\x00\x00\x00mdta\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"

    keys_entries = bytearray()
    for k, _ in kv_pairs:
        kb = k.encode("utf-8")
        keys_entries.extend(struct.pack(">I4s", 8 + len(kb), b"mdta") + kb)
    keys_atom = struct.pack(">I4sII", 16 + len(keys_entries), b"keys", 0, len(kv_pairs)) + bytes(keys_entries)

    ilst_entries = bytearray()
    for idx_k, (_, v) in enumerate(kv_pairs, start=1):
        vb = v.encode("utf-8")
        data_atom = struct.pack(">I4sII", 16 + len(vb), b"data", 1, 0) + vb
        item_atom = struct.pack(">II", 8 + len(data_atom), idx_k) + data_atom
        ilst_entries.extend(item_atom)
    ilst_atom = struct.pack(">I4s", 8 + len(ilst_entries), b"ilst") + bytes(ilst_entries)

    meta_payload = b"\x00\x00\x00\x00" + hdlr_atom + keys_atom + ilst_atom
    meta_atom = struct.pack(">I4s", 8 + len(meta_payload), b"meta") + meta_payload
    udta_atom = struct.pack(">I4s", 8 + len(meta_atom), b"udta") + meta_atom
    return udta_atom


def apply_server_side_hq_bypass_atoms(filepath):
    """
    Server-Side High Quality Fast-Import Bypass (Supports 1080P, 2K, 4K, and up to 120 FPS):
    1. Fixes MP4 'tkhd' atom at exact offset (+80/+84 for v0, +92/+96 for v1) to 1080x1920 / 1920x1080
       without corrupting the transformation matrix at +76.
    2. Patches 'avcC' (H.264 Level -> 4.1 / 0x29) and 'hvcC' (HEVC Level -> 5.0 / 0x96).
    3. Injects ByteDance/Aweme Fast-Import 'udta' atom (te_is_fast_import=1, Hw=1, sourceAppID=1233)
       into 'moov' and updates 'stco'/'co64' chunk offsets so TikTok accepts 100% original video quality!
    """
    try:
        with open(filepath, "rb") as f:
            data = bytearray(f.read())

        limit = len(data) - 92
        i = 0
        target_w, target_h = 1080, 1920
        while i < limit:
            if data[i:i+4] == b"tkhd":
                version = data[i + 4]
                width_offset = (i + 92) if version == 1 else (i + 80)
                height_offset = width_offset + 4
                if height_offset + 4 <= len(data):
                    w_fixed = struct.unpack(">I", data[width_offset:width_offset+4])[0]
                    h_fixed = struct.unpack(">I", data[height_offset:height_offset+4])[0]
                    w = w_fixed >> 16
                    h = h_fixed >> 16
                    if w > 0 and h > 0:
                        if (w > 1080 or h > 1920) and w <= 7680 and h <= 7680:
                            if h >= w:
                                target_w, target_h = 1080, 1920
                            else:
                                target_w, target_h = 1920, 1080
                            struct.pack_into(">I", data, width_offset, target_w << 16)
                            struct.pack_into(">I", data, height_offset, target_h << 16)
                        elif w < 360 or h < 360:
                            if h >= w:
                                target_w, target_h = 1080, 1920
                            else:
                                target_w, target_h = 1920, 1080
                            struct.pack_into(">I", data, width_offset, target_w << 16)
                            struct.pack_into(">I", data, height_offset, target_h << 16)
                        else:
                            target_w, target_h = w, h
                i += 80
            elif data[i:i+4] in (b"hvc1", b"hev1", b"avc1"):
                # Fix broken SampleEntry coded_width / coded_height (0x0 -> 352x288 fallback in TikTok!)
                if i >= 4 and i + 32 <= len(data):
                    box_sz = struct.unpack(">I", data[i-4:i])[0]
                    if 86 <= box_sz <= 16384 and data[i+4:i+10] == b"\x00\x00\x00\x00\x00\x00":
                        cw, ch = struct.unpack(">HH", data[i+28:i+32])
                        if cw == 0 or ch == 0 or cw < 360 or ch < 360 or cw > 1920 or ch > 1920:
                            struct.pack_into(">HH", data, i + 28, target_w, target_h)
                i += 4
            elif data[i:i+4] == b"avcC":
                if i + 8 < len(data) and data[i + 4] == 1:
                    if data[i + 7] > 0x29:
                        data[i + 7] = 0x29
                i += 8
            elif data[i:i+4] == b"hvcC":
                if i + 17 < len(data) and data[i + 4] == 1:
                    if data[i + 16] > 0x96:
                        data[i + 16] = 0x96
                i += 16
            else:
                i += 1

        # Inject ByteDance Fast-Import udta into moov
        pos = 0
        end = len(data)
        moov_pos = -1
        moov_size = 0
        mdat_pos = -1
        while pos + 8 <= end:
            sz = struct.unpack(">I", data[pos:pos+4])[0]
            tp = bytes(data[pos+4:pos+8])
            if sz == 1:
                sz = struct.unpack(">Q", data[pos+8:pos+16])[0]
            elif sz == 0:
                sz = end - pos
            if tp == b"moov":
                moov_pos = pos
                moov_size = sz
            elif tp == b"mdat":
                mdat_pos = pos
            if sz < 8:
                break
            pos += sz

        if moov_pos != -1:
            udta_new = build_bytedance_fast_import_udta(1080, 1920, 15000)
            moov_end = moov_pos + moov_size
            cpos = moov_pos + 8
            new_moov_children = bytearray()
            while cpos + 8 <= moov_end:
                csz = struct.unpack(">I", data[cpos:cpos+4])[0]
                ctp = bytes(data[cpos+4:cpos+8])
                if csz == 0:
                    csz = moov_end - cpos
                if csz < 8 or cpos + csz > moov_end:
                    break
                if ctp != b"udta":
                    new_moov_children.extend(data[cpos:cpos+csz])
                cpos += csz

            new_moov_children.extend(udta_new)
            new_moov_size = 8 + len(new_moov_children)
            delta = new_moov_size - moov_size

            if mdat_pos != -1 and moov_pos < mdat_pos and delta != 0:
                def fix_offsets(buf, start, end_idx):
                    p = start
                    containers = {b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}
                    while p + 8 <= end_idx:
                        bsz = struct.unpack(">I", buf[p:p+4])[0]
                        btp = bytes(buf[p+4:p+8])
                        if bsz < 8 or p + bsz > end_idx:
                            break
                        if btp in containers:
                            fix_offsets(buf, p + 8, p + bsz)
                        elif btp == b"stco":
                            cnt = struct.unpack(">I", buf[p+12:p+16])[0]
                            for k in range(cnt):
                                op = p + 16 + k * 4
                                if op + 4 <= p + bsz:
                                    old_v = struct.unpack(">I", buf[op:op+4])[0]
                                    struct.pack_into(">I", buf, op, old_v + delta)
                        elif btp == b"co64":
                            cnt = struct.unpack(">I", buf[p+12:p+16])[0]
                            for k in range(cnt):
                                op = p + 16 + k * 8
                                if op + 8 <= p + bsz:
                                    old_v = struct.unpack(">Q", buf[op:op+8])[0]
                                    struct.pack_into(">Q", buf, op, old_v + delta)
                        p += bsz
                fix_offsets(new_moov_children, 0, len(new_moov_children))

            new_moov = struct.pack(">I4s", new_moov_size, b"moov") + bytes(new_moov_children)
            out_data = bytearray()
            out_data.extend(data[:moov_pos])
            out_data.extend(new_moov)
            out_data.extend(data[moov_end:])
            data = out_data

        random_seed = os.urandom(16)
        free_box = struct.pack(">I4s16s", 24, b"free", random_seed)
        data.extend(free_box)

        with open(filepath, "wb") as f:
            f.write(data)
    except Exception as e:
        print(f"[warn] apply_server_side_hq_bypass_atoms error: {e}")


V7_PATCH_SCRIPT = r"""
(function() {
  'use strict';

  window.__XUQING_BYPASS_ACTIVE__ = true;
  window.__XUQING_WM_ACTIVE__ = true;

  window.addEventListener('message', function(e) {
    if (!e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'FPS_SET_ACTIVE') {
      window.__XUQING_BYPASS_ACTIVE__ = !!e.data.value;
    } else if (e.data.type === 'WM_SET_ACTIVE') {
      window.__XUQING_WM_ACTIVE__ = !!e.data.value;
    }
  });

  // 1. Disable Browser-Side WebCodecs Downscaling ONLY when High Quality Bypass is ON
  //    (Does NOT touch window.fetch or XMLHttpRequest, preserving 100% native X-Bogus / X-Gnarly / WebMSSDK trust score!)
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

  // 2. Exact MP4 Atom Patch (Preserves Quality_3_fixed_exact_q2_style.mp4 structure 100% without injecting static MD5 udta!):
  //    - Restores stsd -> hvc1 / avc1 coded_width & coded_height (0x0 -> 1080x1920 from tkhd)
  //    - Ensures tkhd is 1080x1920 (or 1920x1080)
  //    - Ensures avcC / hvcC level is compliant
  //    - Refreshes trailing [free] box with a unique per-upload timestamp & hash so every upload has a 100% unique file hash!
  function patchMp4AtomsCleanly(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const limit = Math.min(bytes.byteLength - 92, 512 * 1024);
    let targetW = 1080;
    let targetH = 1920;
    for (let i = 0; i < limit; i++) {
      if (bytes[i] === 0x74 && bytes[i+1] === 0x6B && bytes[i+2] === 0x68 && bytes[i+3] === 0x64) {
        const version = bytes[i + 4];
        const widthOffset = version === 1 ? (i + 92) : (i + 80);
        const heightOffset = widthOffset + 4;
        if (heightOffset + 4 <= bytes.byteLength) {
          const wFixed = view.getUint32(widthOffset, false);
          const hFixed = view.getUint32(heightOffset, false);
          const w = wFixed >>> 16;
          const h = hFixed >>> 16;
          if (w > 0 && h > 0) {
            if (w < 360 || h < 360) {
              targetW = h >= w ? 1080 : 1920;
              targetH = h >= w ? 1920 : 1080;
              view.setUint32(widthOffset, targetW << 16, false);
              view.setUint32(heightOffset, targetH << 16, false);
            } else {
              // Preserve any resolution (1440x1080, 1080x1440, 1000x2166, 1080x1920, etc.) as-is!
              targetW = w;
              targetH = h;
            }
          }
        }
      }
      // Restore stsd -> hvc1 / hev1 / avc1 SampleEntry coded_width & coded_height from 0x0 to 1080x1920
      else if (
        (bytes[i] === 0x68 && bytes[i+1] === 0x76 && bytes[i+2] === 0x63 && bytes[i+3] === 0x31) ||
        (bytes[i] === 0x68 && bytes[i+1] === 0x65 && bytes[i+2] === 0x76 && bytes[i+3] === 0x31) ||
        (bytes[i] === 0x61 && bytes[i+1] === 0x76 && bytes[i+2] === 0x63 && bytes[i+3] === 0x31)
      ) {
        if (i >= 4 && i + 32 <= bytes.byteLength) {
          const boxSz = view.getUint32(i - 4, false);
          const isReservedZero = (
            bytes[i+4] === 0 && bytes[i+5] === 0 && bytes[i+6] === 0 &&
            bytes[i+7] === 0 && bytes[i+8] === 0 && bytes[i+9] === 0
          );
          if (boxSz >= 86 && boxSz <= 16384 && isReservedZero) {
            const cw = view.getUint16(i + 28, false);
            const ch = view.getUint16(i + 30, false);
            if (cw === 0 || ch === 0 || cw < 360 || ch < 360 || cw > 4096 || ch > 4096) {
              view.setUint16(i + 28, targetW, false);
              view.setUint16(i + 30, targetH, false);
            }
          }
        }
      }
      else if (bytes[i] === 0x61 && bytes[i+1] === 0x76 && bytes[i+2] === 0x63 && bytes[i+3] === 0x43) {
        if (i + 8 < bytes.byteLength && bytes[i + 4] === 1) {
          if (bytes[i + 7] > 0x29) {
            bytes[i + 7] = 0x29;
          }
        }
      }
      else if (bytes[i] === 0x68 && bytes[i+1] === 0x76 && bytes[i+2] === 0x63 && bytes[i+3] === 0x43) {
        if (i + 17 < bytes.byteLength && bytes[i + 4] === 1) {
          if (bytes[i + 16] > 0x96) {
            bytes[i + 16] = 0x96;
          }
        }
      }
    }

    // If the file ends with our 69-byte ZILEM_OPTIMIZED free box, randomize the timestamp & ID in-place
    // so every single upload has a 100% unique binary hash while keeping the exact 69-byte box structure!
    if (bytes.byteLength >= 69) {
      const tailOff = bytes.byteLength - 69;
      if (
        bytes[tailOff] === 0 && bytes[tailOff+1] === 0 && bytes[tailOff+2] === 0 && bytes[tailOff+3] === 0x45 &&
        bytes[tailOff+4] === 0x66 && bytes[tailOff+5] === 0x72 && bytes[tailOff+6] === 0x65 && bytes[tailOff+7] === 0x65
      ) {
        const nowIso = new Date().toISOString(); // 24 chars e.g. 2026-10-16T10:15:30.123Z
        const randId = String(Math.floor(1000 + Math.random() * 9000));
        const str = 'ZILEM_OPTIMIZED/1 ' + nowIso + ' ' + randId + ' (2)_zilem.mp4';
        const enc = new TextEncoder().encode(str);
        if (enc.byteLength === 61) {
          bytes.set(enc, tailOff + 8);
        }
      }
    }
  }

  window.V7Patch = async function V7Patch(file) {
    const ab = await file.arrayBuffer();
    if (!window.__XUQING_BYPASS_ACTIVE__) {
      return ab;
    }
    const bytes = new Uint8Array(ab);
    try {
      patchMp4AtomsCleanly(bytes);
    } catch (_) {}
    return bytes.buffer;
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

# WATERMARK_SCRIPT:
# Clean no-op by default so TikTok's Post button and /item/create X-Bogus signature are NEVER intercepted or flagged by Bot/Spam filters!
WATERMARK_SCRIPT = r"""
(function() {
  'use strict';
})();
"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>xuqingxinnocent — Remote Extension Control Panel</title>
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
    <p class="sub">Remote Extension Lock, Force Update & Original HQ Quality Switch</p>

    <div class="row">
      <label>Admin Key (Default: xuqing123)</label>
      <input type="password" id="adminKey" value="xuqing123">
    </div>

    <div class="toggle-row">
      <span>🔴 Block / Force Update Overlay</span>
      <input type="checkbox" id="blocked">
    </div>

    <div class="toggle-row">
      <span>⚡ Enable High Quality Bypass (V7Patch)</span>
      <input type="checkbox" id="enableV7Patch" checked>
    </div>



    <div class="row">
      <label>Minimum Allowed Version (e.g. 6.1.0 — set to 6.2.0 to force update)</label>
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
        hqQualityModel: true,
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
        "server": "xuqingxinnocent Self-Hosted Original HQ Quality (1080P/2K/4K/120FPS) Server",
        "supported_specs": ["Original Quality Passthrough", "1080P", "2K (1440P)", "4K (2160P)", "60FPS", "90FPS", "120FPS"],
        "admin_panel": "/admin",
        "extension_blocked": cfg.get("blocked", False),
        "bypass_enabled": cfg.get("enableV7Patch", True),
        "hq_quality_model": cfg.get("hqQualityModel", True),
        "min_version": cfg.get("min_version", "6.1.0"),
        "endpoints": ["/api/checkup", "/api/control", "/api/mention_info", "/api/patch", "/api/inject", "/api/sign", "/api/transcode", "/admin"],
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })


@app.route("/admin", methods=["GET"])
def admin_panel():
    return Response(ADMIN_HTML, mimetype="text/html")


@app.route("/api/mention_info", methods=["GET"])
def api_mention_info():
    cfg = load_remote_config()
    info = resolve_tiktok_user_from_server("xuqingxinnocent")
    user_id = cfg.get("xuqing_user_id") or info.get("user_id") or ""
    sec_uid = cfg.get("xuqing_sec_uid") or info.get("sec_uid") or ""
    return jsonify({
        "unique_id": "xuqingxinnocent",
        "user_id": user_id,
        "sec_uid": sec_uid
    })


@app.route("/api/admin/config", methods=["POST"])
def api_admin_config():
    body = request.get_json(silent=True) or {}
    if body.get("key") != ADMIN_KEY:
        return jsonify({"ok": False, "error": "Invalid Admin Key"}), 403

    updates = {}
    for k in ["blocked", "update_required", "enableV7Patch", "hqQualityModel", "min_version", "info_message", "update_message", "update_url", "tele", "update_url_2", "wa", "xuqing_user_id", "xuqing_sec_uid"]:
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
        "maxFps": 120,
        "resolutions": ["original", "1080p", "2k", "4k"]
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




EXACT_AVC1_FTYP_32B = bytes.fromhex("000000206674797069736f6d0000020069736f6d69736f32617663316d703431")
EXACT_MVHD_120B = bytes.fromhex("000000786d7668640100000000000000000000000000000000000000000003e8ffffffffffffffff0001000001000000000000000000000000010000000000000000000000000000000100000000000000000000000000004000000000000000000000000000000000000000000000000000000000000003")
EXACT_COLR_19B = bytes.fromhex("00000013636f6c726e636c7800010001000100")
EXACT_PASP_16B = bytes.fromhex("00000010706173700000000100000001")
EXACT_BTRT_20B = bytes.fromhex("00000014627472740000000001312d0001312d00")

def build_apple_ilst_udta(method_name="xuqingxinnocent"):
    clean_name = (method_name or "xuqingxinnocent").strip()
    ver_name = f"{clean_name}-v2.0.0" if not clean_name.endswith("-v2.0.0") else clean_name
    items = [
        (bytes.fromhex("a9415254"), clean_name.encode("utf-8")),
        (bytes.fromhex("a96e616d"), clean_name.encode("utf-8")),
        (bytes.fromhex("a9746f6f"), ver_name.encode("utf-8")),
        (bytes.fromhex("a9656e63"), ver_name.encode("utf-8")),
        (bytes.fromhex("a9777274"), clean_name.encode("utf-8")),
        (bytes.fromhex("a9636d74"), ver_name.encode("utf-8")),
        (b"cprt",                   ver_name.encode("utf-8")),
    ]
    hdlr_box = bytes.fromhex("0000002168646c7200000000000000006d6469726170706c000000000000000000")
    ilst_children = bytearray()
    for tag4, val_bytes in items:
        data_box = struct.pack(">I4sII", 16 + len(val_bytes), b"data", 1, 0) + val_bytes
        item_box = struct.pack(">I4s", 8 + len(data_box), tag4) + data_box
        ilst_children.extend(item_box)
    ilst_box = struct.pack(">I4s", 8 + len(ilst_children), b"ilst") + bytes(ilst_children)
    meta_payload = bytes(4) + hdlr_box + ilst_box
    meta_box = struct.pack(">I4s", 8 + len(meta_payload), b"meta") + meta_payload
    udta_box = struct.pack(">I4s", 8 + len(meta_box), b"udta") + meta_box
    return udta_box

def apply_exact_xuqing_1000x2166_structure(filepath, target_w=1000, target_h=2166, method_name="xuqingxinnocent"):
    with open(filepath, "rb") as f:
        raw = f.read()

    pos = 0
    end = len(raw)
    moov_bytes = None
    mdat_pos = -1
    mdat_bytes = None
    while pos + 8 <= end:
        sz = struct.unpack(">I", raw[pos:pos+4])[0]
        tp = raw[pos+4:pos+8]
        if sz == 1:
            sz = struct.unpack(">Q", raw[pos+8:pos+16])[0]
        elif sz == 0:
            sz = end - pos
        if sz < 8 or pos + sz > end:
            break
        if tp == b"moov":
            moov_bytes = raw[pos:pos+sz]
        elif tp == b"mdat":
            mdat_pos = pos
            mdat_bytes = raw[pos:pos+sz]
            break
        pos += sz

    if not moov_bytes or not mdat_bytes:
        return "Failed: moov or mdat not found"

    def rebuild_box(box_data, is_video_trak=False):
        btype = box_data[4:8]
        if btype == b"trak":
            is_video_trak = (b"vmhd" in box_data)

        if btype == b"tkhd":
            out_tkhd = bytearray(box_data)
            ver = out_tkhd[8]
            w_off = 96 if ver == 1 else 84
            if w_off + 8 <= len(out_tkhd):
                if is_video_trak:
                    struct.pack_into(">II", out_tkhd, w_off, target_w << 16, target_h << 16)
                else:
                    struct.pack_into(">II", out_tkhd, w_off, 0, 0)
            return bytes(out_tkhd)

        if btype == b"stsd" and is_video_trak:
            if len(box_data) >= 102:
                entry_type = box_data[20:24]
                if entry_type == b"avc1":
                    avcc_box = b""
                    btrt_box = EXACT_BTRT_20B
                    sub_pos = 16 + 86
                    sub_end = len(box_data)
                    while sub_pos + 8 <= sub_end:
                        ssz = struct.unpack(">I", box_data[sub_pos:sub_pos+4])[0]
                        stp = box_data[sub_pos+4:sub_pos+8]
                        if ssz < 8 or sub_pos + ssz > sub_end:
                            break
                        if stp == b"avcC":
                            avcc_box = box_data[sub_pos:sub_pos+ssz]
                        elif stp == b"btrt" and ssz == 20:
                            btrt_box = box_data[sub_pos:sub_pos+ssz]
                        sub_pos += ssz

                    if avcc_box:
                        avc1_hdr = bytearray(box_data[16:16+86])
                        struct.pack_into(">HH", avc1_hdr, 32, target_w, target_h)
                        avc1_hdr[50:82] = bytes(32)
                        avc1_children = avcc_box + EXACT_COLR_19B + EXACT_PASP_16B + btrt_box
                        avc1_size = 86 + len(avc1_children)
                        struct.pack_into(">I", avc1_hdr, 0, avc1_size)
                        avc1_box = bytes(avc1_hdr) + avc1_children
                        return struct.pack(">I4sII", 16 + len(avc1_box), b"stsd", 0, 1) + avc1_box

        containers = {b"trak", b"mdia", b"minf", b"stbl"}
        if btype not in containers:
            return box_data

        cpos = 8
        cend = len(box_data)
        children = bytearray()
        while cpos + 8 <= cend:
            csz = struct.unpack(">I", box_data[cpos:cpos+4])[0]
            if csz < 8 or cpos + csz > cend:
                break
            children.extend(rebuild_box(box_data[cpos:cpos+csz], is_video_trak=is_video_trak))
            cpos += csz
        return struct.pack(">I4s", 8 + len(children), btype) + bytes(children)

    mpos = 8
    mend = len(moov_bytes)
    new_moov_children = bytearray()
    new_moov_children.extend(EXACT_MVHD_120B)

    while mpos + 8 <= mend:
        msz = struct.unpack(">I", moov_bytes[mpos:mpos+4])[0]
        mtp = moov_bytes[mpos+4:mpos+8]
        if msz < 8 or mpos + msz > mend:
            break
        if mtp not in (b"mvhd", b"udta"):
            new_moov_children.extend(rebuild_box(moov_bytes[mpos:mpos+msz]))
        mpos += msz

    udta_box = build_apple_ilst_udta(method_name)
    new_moov_children.extend(udta_box)
    new_moov_size = 8 + len(new_moov_children)

    new_mdat_pos = len(EXACT_AVC1_FTYP_32B) + new_moov_size
    delta = new_mdat_pos - mdat_pos

    if delta != 0:
        def shift_stco(buf, start, end_idx):
            cur = start
            containers = {b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}
            while cur + 8 <= end_idx:
                bsz = struct.unpack(">I", buf[cur:cur+4])[0]
                btp = bytes(buf[cur+4:cur+8])
                if bsz < 8 or cur + bsz > end_idx:
                    break
                if btp in containers:
                    shift_stco(buf, cur + 8, cur + bsz)
                elif btp == b"stco":
                    cnt = struct.unpack(">I", buf[cur+12:cur+16])[0]
                    for k in range(cnt):
                        op = cur + 16 + k * 4
                        if op + 4 <= cur + bsz:
                            old_v = struct.unpack(">I", buf[op:op+4])[0]
                            struct.pack_into(">I", buf, op, old_v + delta)
                elif btp == b"co64":
                    cnt = struct.unpack(">I", buf[cur+12:cur+16])[0]
                    for k in range(cnt):
                        op = cur + 16 + k * 8
                        if op + 8 <= cur + bsz:
                            old_v = struct.unpack(">Q", buf[op:op+8])[0]
                            struct.pack_into(">Q", buf, op, old_v + delta)
                cur += bsz
        shift_stco(new_moov_children, 0, len(new_moov_children))

    new_moov_box = struct.pack(">I4s", new_moov_size, b"moov") + bytes(new_moov_children)

    with open(filepath, "wb") as f:
        f.write(EXACT_AVC1_FTYP_32B)
        f.write(new_moov_box)
        f.write(mdat_bytes)

    return f"Rebuilt 1000x2166 H.264 HQ (moov={new_moov_size}B)"


@app.route("/api/transcode", methods=["POST", "OPTIONS"])
def api_transcode():
    if request.method == "OPTIONS":
        return add_cors(make_response("", 204))

    if "video" not in request.files:
        return jsonify({"error": "No video file uploaded"}), 400

    f = request.files["video"]
    job_id = str(uuid.uuid4())[:8]
    in_path = os.path.join(TEMP_DIR, f"in_{job_id}.mp4")
    out_path = os.path.join(TEMP_DIR, f"out_{job_id}.mp4")
    f.save(in_path)

    target_res = (request.form.get("target_res") or request.args.get("target_res") or "1000x2166").strip()
    vinfo = probe_video_info(in_path)
    in_w = int(vinfo.get("width", 1000) or 1000)
    in_h = int(vinfo.get("height", 2166) or 2166)
    is_landscape = in_w > in_h
    if target_res == "1080x1920":
        target_w, target_h = (1920, 1080) if is_landscape else (1080, 1920)
    elif target_res == "1440x1080":
        target_w, target_h = (1440, 1080) if is_landscape else (1080, 1440)
    elif target_res in ("auto", "original"):
        target_w, target_h = (in_w, in_h)
    else:
        target_w, target_h = (2166, 1000) if is_landscape else (1000, 2166)

    vf_chain = (
        f"scale={target_w}:{target_h}:flags=lanczos+accurate_rnd+full_chroma_int,"
        "setsar=1/1,"
        "fps=60,"
        "format=yuv420p"
    )

    x264_params = (
        "ref=1:bframes=1:b-adapt=0:keyint=30:min-keyint=15:scenecut=40:"
        "nal-hrd=none:colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=off"
    )

    cmd = [
        "ffmpeg", "-y",
        "-threads", "2",
        "-i", in_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf", vf_chain,
        "-fps_mode", "cfr",
        "-r", "60",
        "-video_track_timescale", "90000",
        "-c:v", "libx264",
        "-tag:v", "avc1",
        "-preset", "superfast",
        "-profile:v", "high",
        "-level:v", "5.2",
        "-b:v", "20000k",
        "-maxrate", "22000k",
        "-bufsize", "40000k",
        "-x264-params", x264_params,
        "-pix_fmt", "yuv420p",
        "-color_range", "tv",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-field_order", "progressive",
        "-brand", "isom",
        "-metadata:s:v:0", "encoder=xuqingxinnocent-v2.0.0",
        "-c:a", "aac",
        "-b:a", "320k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        out_path
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        os.remove(in_path)
    except Exception:
        pass

    if res.returncode != 0 or not os.path.exists(out_path):
        return jsonify({"error": "FFmpeg transcode failed", "details": res.stderr[-1200:]}), 500

    apply_exact_xuqing_1000x2166_structure(out_path, target_w=target_w, target_h=target_h, method_name="xuqingxinnocent")

    with open(out_path, "rb") as out_f:
        out_bytes = out_f.read()

    try:
        os.remove(out_path)
    except Exception:
        pass

    resp = make_response(out_bytes)
    resp.headers["Content-Type"] = "video/mp4"
    resp.headers["Content-Disposition"] = 'attachment; filename="xuqingxinnocent_1000x2166_HQ.mp4"'
    return add_cors(resp)


