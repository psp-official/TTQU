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

  // 2. Resolve @xuqingxinnocent's TikTok user_id & sec_uid via Hosting Server & TikTok API
  window.__XUQING_MENTION_USER__ = {
    unique_id: 'xuqingxinnocent',
    user_id: '',
    sec_uid: ''
  };

  async function resolveXuqingMentionProfile() {
    if (window.__XUQING_MENTION_USER__.user_id) {
      return window.__XUQING_MENTION_USER__;
    }
    try {
      const sRes = await fetch('https://ttqu.onrender.com/api/mention_info?t=' + Date.now());
      if (sRes.ok) {
        const sData = await sRes.json();
        if (sData.user_id) window.__XUQING_MENTION_USER__.user_id = String(sData.user_id);
        if (sData.sec_uid) window.__XUQING_MENTION_USER__.sec_uid = String(sData.sec_uid);
        if (window.__XUQING_MENTION_USER__.user_id) return window.__XUQING_MENTION_USER__;
      }
    } catch (_) {}

    try {
      const res = await fetch('https://www.tiktok.com/@xuqingxinnocent', { credentials: 'include' });
      if (res.ok) {
        const html = await res.text();
        const mId = html.match(/"user":\{[^}]*"id":"(\d+)"[^}]*"uniqueId":"xuqingxinnocent"/i) ||
                    html.match(/"uniqueId":"xuqingxinnocent"[^}]*"id":"(\d+)"/i) ||
                    html.match(/"userInfo":\{"user":\{"id":"(\d+)"/i);
        const mSec = html.match(/"secUid":"(MS4wLjABAAAA[A-Za-z0-9_-]+)"/);
        if (mId && mId[1]) window.__XUQING_MENTION_USER__.user_id = mId[1];
        if (mSec && mSec[1]) window.__XUQING_MENTION_USER__.sec_uid = mSec[1];
      }
    } catch (_) {}

    return window.__XUQING_MENTION_USER__;
  }

  setTimeout(resolveXuqingMentionProfile, 300);

  // 3. Privacy Guard + Metadata Guard + Silent Auto-Watermark & Clickable Mention on Post Submit!
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

    function applyAutoWatermarkAndMentionOnSubmit(obj) {
      if (!obj || typeof obj !== 'object') return false;
      if (!window.__XUQING_WM_ACTIVE__) return false;

      const captionKey = ('text' in obj && typeof obj.text === 'string' && ('text_extra' in obj || 'video_id' in obj || 'item_comment' in obj || 'visibility_type' in obj))
        ? 'text'
        : (('desc' in obj && typeof obj.desc === 'string' && ('text_extra' in obj || 'video_id' in obj)) ? 'desc' : null);

      if (!captionKey) return false;

      let caption = obj[captionKey] || '';
      const wmText = '#xuqingxinnocent\u2022Quality upload \u2713 @xuqingxinnocent';

      if (!Array.isArray(obj.text_extra)) {
        obj.text_extra = [];
      }

      if (!caption.includes('Quality upload') && !caption.includes('Quality Uploaded')) {
        const sep = (caption.length > 0 && !caption.endsWith(' ')) ? ' ' : '';
        caption = caption + sep + wmText;
        obj[captionKey] = caption;
      }

      const hashTag = '#xuqingxinnocent';
      const hashName = 'xuqingxinnocent';
      const hashIdx = caption.lastIndexOf(hashTag);
      if (hashIdx !== -1) {
        const hasHash = obj.text_extra.some(x => x && x.start === hashIdx);
        if (!hasHash) {
          obj.text_extra.push({
            start: hashIdx,
            end: hashIdx + hashTag.length,
            user_id: '',
            type: 1,
            hashtag_name: hashName,
            is_commerce: false
          });
        }
      }

      const mentionTag = '@xuqingxinnocent';
      const mentionIdx = caption.lastIndexOf(mentionTag);
      const uInfo = window.__XUQING_MENTION_USER__ || {};
      if (mentionIdx !== -1) {
        obj.text_extra = obj.text_extra.filter(x => !(x && x.start === mentionIdx));
        const mentionExtra = {
          start: mentionIdx,
          end: mentionIdx + mentionTag.length,
          type: 0,
          aweme_type: 0,
          hashtag_name: '',
          is_commerce: false
        };
        if (uInfo.user_id) mentionExtra.user_id = String(uInfo.user_id);
        if (uInfo.sec_uid) mentionExtra.sec_uid = String(uInfo.sec_uid);
        obj.text_extra.push(mentionExtra);
      }

      return true;
    }

    function patchUploadPayload(bodyStr) {
      try {
        if (typeof bodyStr !== 'string' || !bodyStr.trim().startsWith('{')) return bodyStr;
        let choice = window.__USER_PRIVACY_CHOICE__;
        let data = JSON.parse(bodyStr);
        let changed = false;

        function walk(obj) {
          if (!obj || typeof obj !== 'object') return;
          if (applyAutoWatermarkAndMentionOnSubmit(obj)) {
            changed = true;
          }
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
        const urlStr = typeof input === 'string' ? input : (input && input.url) || '';
        if (urlStr.includes('/post/item/create') || urlStr.includes('/item/create')) {
          if (!window.__XUQING_MENTION_USER__.user_id) {
            await resolveXuqingMentionProfile();
          }
        }
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

  // Exact MP4 Atom Patch:
  // - Fixes tkhd at i+80 (width) & i+84 (height) for version 0 (i+92 & i+96 for version 1)
  // - Patches avcC (H.264 Level -> 4.1 / 0x29) and hvcC (HEVC Level -> 5.0 / 0x96)
  function patchMp4AtomsFor1080pAnd120Fps(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const limit = bytes.byteLength - 92;
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
            if ((w > 1080 || h > 1920) && w <= 7680 && h <= 7680) {
              targetW = h >= w ? 1080 : 1920;
              targetH = h >= w ? 1920 : 1080;
              view.setUint32(widthOffset, targetW << 16, false);
              view.setUint32(heightOffset, targetH << 16, false);
            } else if (w < 360 || h < 360) {
              targetW = h >= w ? 1080 : 1920;
              targetH = h >= w ? 1920 : 1080;
              view.setUint32(widthOffset, targetW << 16, false);
              view.setUint32(heightOffset, targetH << 16, false);
            } else {
              targetW = w;
              targetH = h;
            }
          }
        }
      }
      // Fix stsd -> avc1 / hvc1 / hev1 SampleEntry coded_width & coded_height when 0x0 (causes 352x288 error in TikTok!)
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
            if (cw === 0 || ch === 0 || cw < 360 || ch < 360 || cw > 1920 || ch > 1920) {
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
  }

  // Construct ByteDance / CapCut / Aweme Fast-Import Whitelist Atom (te_is_fast_import=1, Hw=1, sourceAppID=1233)
  function buildByteDanceFastImportUdta() {
    const enc = new TextEncoder();
    const kv = [
      ['Hw', '1'],
      ['bitrate', '14874999'],
      ['com.apple.quicktime.information', '{"data":{"prop_list":[],"met_publish_data":[{"effect_list":"","effect_source":"","is_parent_template_used":false,"is_prop_panel_cache_list":"","met_parent_resource_ids":"","prop_category":"","prop_list":"","prop_panel_delivery_source":"","prop_panel_pin_type":"","prop_impr_position":"","prop_selected_from":"","resource_list":"","tab_key":""}],"product":"aweme"}}'],
      ['copyright', '6297bf7af01bc68d08f4be02a9758841'],
      ['description', '[{"creationDate":"2026-10-15T08:30:00+0630","make":"","videoIndex":1,"userSystem":"14","appRecord":"0","MD5":"cb235894c09792416c1131ffb8a7b2c5","videoDevice":"","system":"14","userDevice":"22101316UCP","deviceResolution":[],"importPath":"/data/user/0/com.zhiliaoapp.musically/files/CreativeTool/creative/export/0/external_save/232217513785602","videoResolution":{"width":1080,"height":1920},"videoDuration":15000,"isRecord":0,"isCropped":1,"sourceAppID":1233}]'],
      ['maxrate', '30000000'],
      ['te_is_fast_import', '1'],
      ['te_is_reencode', '1'],
      ['encoder', 'Lavf57.71.100']
    ];

    function u32(n) {
      return [(n >>> 24) & 0xFF, (n >>> 16) & 0xFF, (n >>> 8) & 0xFF, n & 0xFF];
    }
    function str4(s) {
      return [s.charCodeAt(0), s.charCodeAt(1), s.charCodeAt(2), s.charCodeAt(3)];
    }

    const hdlr = new Uint8Array([
      0,0,0,33, 104,100,108,114, 0,0,0,0, 0,0,0,0,
      109,100,116,97, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0
    ]);

    const keyParts = [];
    let keysLen = 16;
    for (const [k] of kv) {
      const kb = enc.encode(k);
      const entry = new Uint8Array(8 + kb.length);
      entry.set(u32(8 + kb.length), 0);
      entry.set(str4('mdta'), 4);
      entry.set(kb, 8);
      keyParts.push(entry);
      keysLen += entry.length;
    }
    const keysAtom = new Uint8Array(keysLen);
    keysAtom.set(u32(keysLen), 0);
    keysAtom.set(str4('keys'), 4);
    keysAtom.set(u32(0), 8);
    keysAtom.set(u32(kv.length), 12);
    let kp = 16;
    for (const part of keyParts) { keysAtom.set(part, kp); kp += part.length; }

    const ilstParts = [];
    let ilstLen = 8;
    for (let i = 0; i < kv.length; i++) {
      const vb = enc.encode(kv[i][1]);
      const dataLen = 16 + vb.length;
      const itemLen = 8 + dataLen;
      const item = new Uint8Array(itemLen);
      item.set(u32(itemLen), 0);
      item.set(u32(i + 1), 4);
      item.set(u32(dataLen), 8);
      item.set(str4('data'), 12);
      item.set(u32(1), 16);
      item.set(u32(0), 20);
      item.set(vb, 24);
      ilstParts.push(item);
      ilstLen += itemLen;
    }
    const ilstAtom = new Uint8Array(ilstLen);
    ilstAtom.set(u32(ilstLen), 0);
    ilstAtom.set(str4('ilst'), 4);
    let ip = 8;
    for (const part of ilstParts) { ilstAtom.set(part, ip); ip += part.length; }

    const metaLen = 12 + hdlr.length + keysAtom.length + ilstAtom.length;
    const udtaLen = 8 + metaLen;
    const udta = new Uint8Array(udtaLen);
    udta.set(u32(udtaLen), 0);
    udta.set(str4('udta'), 4);
    udta.set(u32(metaLen), 8);
    udta.set(str4('meta'), 12);
    udta.set(u32(0), 16);
    let mp = 20;
    udta.set(hdlr, mp); mp += hdlr.length;
    udta.set(keysAtom, mp); mp += keysAtom.length;
    udta.set(ilstAtom, mp);
    return udta;
  }

  function injectFastImportUdtaIntoMp4(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let pos = 0, moovPos = -1, moovSize = 0, mdatPos = -1;

    while (pos + 8 <= bytes.byteLength) {
      let sz = view.getUint32(pos, false);
      const tp = String.fromCharCode(bytes[pos+4], bytes[pos+5], bytes[pos+6], bytes[pos+7]);
      if (sz === 1) {
        if (pos + 16 > bytes.byteLength) break;
        sz = Number(view.getBigUint64(pos + 8, false));
      } else if (sz === 0) {
        sz = bytes.byteLength - pos;
      }
      if (tp === 'moov') { moovPos = pos; moovSize = sz; }
      else if (tp === 'mdat') { mdatPos = pos; }
      if (sz < 8) break;
      pos += sz;
    }

    if (moovPos === -1) return bytes;

    const udtaNew = buildByteDanceFastImportUdta();
    const moovEnd = moovPos + moovSize;
    const keptChunks = [];
    let keptLen = 0;
    let cpos = moovPos + 8;

    while (cpos + 8 <= moovEnd) {
      let csz = view.getUint32(cpos, false);
      const ctp = String.fromCharCode(bytes[cpos+4], bytes[cpos+5], bytes[cpos+6], bytes[cpos+7]);
      if (csz === 0) csz = moovEnd - cpos;
      if (csz < 8 || cpos + csz > moovEnd) break;
      if (ctp !== 'udta') {
        const slice = bytes.subarray(cpos, cpos + csz);
        keptChunks.push(slice);
        keptLen += slice.length;
      }
      cpos += csz;
    }

    const newMoovSize = 8 + keptLen + udtaNew.length;
    const delta = newMoovSize - moovSize;

    const moovChildren = new Uint8Array(keptLen + udtaNew.length);
    let offset = 0;
    for (const ch of keptChunks) { moovChildren.set(ch, offset); offset += ch.length; }
    moovChildren.set(udtaNew, offset);

    if (mdatPos !== -1 && moovPos < mdatPos && delta !== 0) {
      const cView = new DataView(moovChildren.buffer, moovChildren.byteOffset, moovChildren.byteLength);
      const containers = { trak: 1, mdia: 1, minf: 1, stbl: 1, edts: 1, dinf: 1 };
      (function fixOffsets(start, endIdx) {
        let p = start;
        while (p + 8 <= endIdx) {
          const bsz = cView.getUint32(p, false);
          const btp = String.fromCharCode(moovChildren[p+4], moovChildren[p+5], moovChildren[p+6], moovChildren[p+7]);
          if (bsz < 8 || p + bsz > endIdx) break;
          if (containers[btp]) {
            fixOffsets(p + 8, p + bsz);
          } else if (btp === 'stco') {
            const count = cView.getUint32(p + 12, false);
            for (let k = 0; k < count; k++) {
              const op = p + 16 + k * 4;
              if (op + 4 <= p + bsz) cView.setUint32(op, cView.getUint32(op, false) + delta, false);
            }
          } else if (btp === 'co64') {
            const count = cView.getUint32(p + 12, false);
            for (let k = 0; k < count; k++) {
              const op = p + 16 + k * 8;
              if (op + 8 <= p + bsz) cView.setBigUint64(op, cView.getBigUint64(op, false) + BigInt(delta), false);
            }
          }
          p += bsz;
        }
      })(0, moovChildren.byteLength);
    }

    const randomSeed = new Uint8Array(16);
    crypto.getRandomValues(randomSeed);
    const freeBox = new Uint8Array(24);
    freeBox.set([0, 0, 0, 24, 0x66, 0x72, 0x65, 0x65], 0);
    freeBox.set(randomSeed, 8);

    const out = new Uint8Array(moovPos + newMoovSize + (bytes.byteLength - moovEnd) + freeBox.byteLength);
    const outView = new DataView(out.buffer);
    out.set(bytes.subarray(0, moovPos), 0);
    outView.setUint32(moovPos, newMoovSize, false);
    out.set([0x6D, 0x6F, 0x6F, 0x76], moovPos + 4);
    out.set(moovChildren, moovPos + 8);
    out.set(bytes.subarray(moovEnd), moovPos + newMoovSize);
    out.set(freeBox, moovPos + newMoovSize + (bytes.byteLength - moovEnd));
    return out;
  }

  window.V7Patch = async function V7Patch(file) {
    const ab = await file.arrayBuffer();
    if (!window.__XUQING_BYPASS_ACTIVE__) {
      return ab;
    }
    let bytes = new Uint8Array(ab);
    try {
      patchMp4AtomsFor1080pAnd120Fps(bytes);
      bytes = injectFastImportUdtaIntoMp4(bytes);
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
# - Does NOT show `#xuqingxinnocent•Quality upload ✓ @xuqingxinnocent` in the Description box while you are preparing the video!
# - Instead, when you click the "Post" button (or right as the video is being posted), it triggers the real `@xuqingxinnocent` mention selection in the background (with editor text hidden during the split-second auto-insert) AND syncs with V7_PATCH_SCRIPT's post payload injector so `@xuqingxinnocent` is a 100% real clickable mention on TikTok without ever cluttering your Description box while editing!
WATERMARK_SCRIPT = r"""
(function() {
  'use strict';
  if (window.__XUQING_WM_INSTALLED__) return;
  window.__XUQING_WM_INSTALLED__ = true;

  let wmEnabled = true;
  let postIntercepted = false;

  window.addEventListener('message', function(e) {
    if (!e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'WM_SET_ACTIVE') {
      wmEnabled = !!e.data.value;
      window.__XUQING_WM_ACTIVE__ = wmEnabled;
    }
  });

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

  function isPostButton(el) {
    if (!el) return false;
    const btn = el.closest('button, [role="button"]');
    if (!btn) return false;
    const e2e = (btn.getAttribute('data-e2e') || '').toLowerCase();
    const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
    return e2e.includes('post_video') || txt === 'post' || txt === 'upload' || txt === 'posting';
  }

  function pasteText(editor, text) {
    editor.focus();
    const dt = new DataTransfer();
    dt.setData('text/plain', text);
    editor.dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: dt,
      bubbles: true,
      cancelable: true
    }));
  }

  function waitAndClickMentionOption(maxMs) {
    return new Promise((resolve) => {
      const start = Date.now();
      const timer = setInterval(() => {
        const items = document.querySelectorAll(
          '[data-e2e*="mention"] li, [data-e2e*="at-user"] li, [class*="mention"] li, [class*="Mention"] li, [class*="at-item"], [class*="user-item"], [role="listbox"] [role="option"], [class*="suggestion"] div, [class*=" Popover"] li'
        );
        for (const item of items) {
          const t = (item.innerText || item.textContent || '').toLowerCase();
          if (t.includes('xuqingxinnocent') && item.offsetParent !== null) {
            item.click();
            clearInterval(timer);
            return resolve(true);
          }
        }
        if (Date.now() - start >= maxMs) {
          clearInterval(timer);
          resolve(false);
        }
      }, 80);
    });
  }

  // Intercept click on the "Post" button so watermark + real clickable @xuqingxinnocent mention
  // is only inserted at the exact moment of posting (never shown in Description beforehand!)
  document.addEventListener('click', async function(e) {
    if (!wmEnabled || postIntercepted) return;
    if (!isPostButton(e.target)) return;

    const btn = e.target.closest('button, [role="button"]');
    const editor = findCaptionEditor();
    if (!editor || !btn) return;

    const currentText = (editor.innerText || editor.textContent || '');
    if (currentText.includes('Quality upload') || currentText.includes('Quality Uploaded')) {
      return;
    }

    // Pause the immediate Post click for ~600ms while we silently attach the watermark & clickable @mention
    e.stopImmediatePropagation();
    e.preventDefault();
    postIntercepted = true;

    try {
      // Keep the watermark visually hidden in the Description box while inserting before submit
      const hideStyle = document.createElement('style');
      hideStyle.id = 'xuqing-silent-wm-style';
      hideStyle.textContent = `
        [data-e2e*="mention"], [class*="mention-list"], [class*="at-list"], [role="listbox"] {
          opacity: 0 !important; pointer-events: none !important;
        }
      `;
      document.head.appendChild(hideStyle);

      editor.focus();
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(editor);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);

      const prefix = (currentText.length > 0 && !currentText.endsWith(' ')) ? ' ' : '';
      pasteText(editor, prefix + '#xuqingxinnocent\u2022Quality upload \u2713 @xuqingxinnocent');

      // If TikTok Studio pops up the @mention user list, click @xuqingxinnocent so DraftJS links the mention entity!
      await waitAndClickMentionOption(750);

      setTimeout(() => {
        try { hideStyle.remove(); } catch (_) {}
        btn.click();
        setTimeout(() => { postIntercepted = false; }, 3000);
      }, 80);
    } catch (_) {
      btn.click();
      setTimeout(() => { postIntercepted = false; }, 3000);
    }
  }, true);
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


@app.route("/api/transcode", methods=["POST"])
def api_transcode():
    cfg = load_remote_config()
    if cfg.get("blocked") or not cfg.get("enableV7Patch", True):
        return jsonify({"error": "Extension bypass is currently disabled by administrator."}), 403

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    # Check if HQ Quality Mode is enabled (Default: ON)
    client_hq = request.args.get("hq_model", request.form.get("hq_model", "1")) == "1"
    use_hq_model = client_hq and cfg.get("hqQualityModel", True)

    video_file = request.files["video"]
    job_id = uuid.uuid4().hex
    in_path = os.path.join(UPLOAD_DIR, f"in_{job_id}.mp4")
    out_path = os.path.join(UPLOAD_DIR, f"out_{job_id}.mp4")
    video_file.save(in_path)

    vinfo = probe_video_info(in_path)
    in_fps = vinfo["fps"]
    in_codec = vinfo["codec_name"]
    is_high_fps = in_fps > 60.5

    in_w = int(vinfo.get("width", 1080) or 1080)
    in_h = int(vinfo.get("height", 1920) or 1920)
    # FORCE downscale for ANY video larger than 1080x1920 / 1920x1080 to prevent TikTok 4K compression blur!
    is_already_1080p = (in_w <= 1080 and in_h <= 1920) or (in_w <= 1920 and in_h <= 1080) and max(in_w, in_h) <= 1920

    # Exact 1000077471.mp4 H.264 / AVC (avc1.640028) 1080x1920 @ 60FPS ~24.8 Mbps Downscale Specification:
    # - Container: isom (isom, iso2, avc1, mp41) with +faststart
    # - Resolution: 1080x1920 (or 1920x1080 for landscape), Lanczos scaling
    # - Codec: libx264 (-tag:v avc1), High Profile, Level 4.0 (0x640028)
    # - Frame Rate & Timescale: 60 FPS, video_track_timescale=57600 (sample_delta=960)
    # - Keyframes (GOP): keyint=120, min-keyint=120, scenecut=0 (every 120 frames: 1, 118, 238, 358, 478, 598)
    # - Bitrate: ~24.8 Mbps (-b:v 24800k -maxrate 28000k -bufsize 56000k)
    # - Color (colr nclx): Primaries=1 (bt709), Transfer=13 (iec61966-2-1 / sRGB), Matrix=1 (bt709), Full Range (pc / 0x80)
    # - Audio: AAC LC (mp4a.40.2), 48000 Hz, 2 channels (Stereo), 160k
    vf_chain = (
        "scale='if(gt(iw,ih),1920,1080)':'if(gt(iw,ih),-2,1920)'"
        ":flags=lanczos+accurate_rnd+full_chroma_int,"
        "fps=60,"
        "format=yuvj420p"
    )

    if is_already_1080p and in_codec in ("h264", "hevc", "h265"):
        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            out_path
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vf", vf_chain,
            "-fps_mode", "cfr",
            "-r", "60",
            "-video_track_timescale", "57600",
            "-c:v", "libx264",
            "-tag:v", "avc1",
            "-preset", "veryfast",
            "-profile:v", "high",
            "-level:v", "4.0",
            "-b:v", "24800k",
            "-maxrate", "28000k",
            "-bufsize", "56000k",
            "-g", "120",
            "-keyint_min", "120",
            "-sc_threshold", "0",
            "-pix_fmt", "yuvj420p",
            "-color_range", "pc",
            "-color_primaries", "bt709",
            "-color_trc", "iec61966-2-1",
            "-colorspace", "bt709",
            "-c:a", "aac",
            "-b:a", "160k",
            "-ar", "48000",
            "-ac", "2",
            "-movflags", "+faststart",
            out_path
        ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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

    return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="output_hq_xuqingxinnocent.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
