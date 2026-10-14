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
    Uses ffprobe to detect input video codec, width, height, bitrate, and frame rate.
    """
    info = {"codec_name": "h264", "width": 1080, "height": 1920, "fps": 60.0, "bit_rate": 0}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,bit_rate",
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
                info["bit_rate"] = int(st.get("bit_rate") or 0)
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
    Server-Side High Quality Bypass:
    1. Ensures MP4 'tkhd' atom matches 1080p dimensions cleanly.
    2. Scans MP4 'avcC' atom and spoofs AVCLevelIndication to Level 4.1 (0x29) so 60FPS/120FPS videos
       pass TikTok's strict High@4.1 stream check without triggering server-side re-encoding.
    3. Appends a unique 24-byte 'free' atom with cryptographic random bytes to bypass hash deduplication.
    """
    try:
        with open(filepath, "rb") as f:
            data = bytearray(f.read())

        limit = len(data) - 92
        i = 0
        while i < limit:
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
            elif data[i:i+4] == b"avcC":
                if i + 8 < len(data) and data[i + 4] == 1:
                    level_byte = data[i + 7]
                    if level_byte > 0x29:
                        data[i + 7] = 0x29
                i += 8
            else:
                i += 1

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

  // 1. Disable Browser-Side WebCodecs Pre-Compression ONLY when High Quality Bypass is ON
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

  // 2. Resolve @xuqingxinnocent's TikTok user_id & sec_uid
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

    if (!window.__XUQING_MENTION_USER__.user_id) {
      try {
        const mRes = await fetch('https://www.tiktok.com/api/discover/search/?keyword=xuqingxinnocent&offset=0&count=5', { credentials: 'include' });
        if (mRes.ok) {
          const mJson = await mRes.json();
          const list = mJson?.user_list || mJson?.data || [];
          for (const item of list) {
            const u = item?.user_info || item?.user || item;
            if (u && String(u.unique_id || u.uniqueId || '').toLowerCase() === 'xuqingxinnocent') {
              if (u.uid || u.id) window.__XUQING_MENTION_USER__.user_id = String(u.uid || u.id);
              if (u.sec_uid || u.secUid) window.__XUQING_MENTION_USER__.sec_uid = String(u.sec_uid || u.secUid);
              break;
            }
          }
        }
      } catch (_) {}
    }

    return window.__XUQING_MENTION_USER__;
  }

  setTimeout(resolveXuqingMentionProfile, 300);

  // 3. Privacy Guard + Metadata Guard + Silent Auto-Watermark & Clickable Mention on Post Submit
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

  function patchMp4AtomsFor1080pAnd120Fps(bytes) {
    const limit = bytes.byteLength - 92;
    for (let i = 0; i < limit; i++) {
      // Spoof avcC level to 4.1 (0x29) so high-FPS 1080p streams pass without re-encoding
      if (bytes[i] === 0x61 && bytes[i+1] === 0x76 && bytes[i+2] === 0x63 && bytes[i+3] === 0x43) {
        if (i + 8 < bytes.byteLength && bytes[i + 4] === 1) {
          if (bytes[i + 7] > 0x29) {
            bytes[i + 7] = 0x29;
          }
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
      patchMp4AtomsFor1080pAnd120Fps(bytes);
    } catch (_) {}

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
    freeBox[4] = 0x66; freeBox[5] = 0x72; freeBox[6] = 0x65; freeBox[7] = 0x65;
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

    e.stopImmediatePropagation();
    e.preventDefault();
    postIntercepted = true;

    try {
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

    <div class="toggle-row">
      <span>💎 Enable HQ Quality Mode (4K/2K Ultra-Sharp Mastering)</span>
      <input type="checkbox" id="hqQualityModel" checked>
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
        "server": "xuqingxinnocent Self-Hosted Original HQ Quality (1080P/2K/4K/120FPS) Server",
        "supported_specs": ["4K/2K Ultra-Sharp Mastering", "1080P", "2K (1440P)", "4K (2160P)", "60FPS", "90FPS", "120FPS"],
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

    client_hq = request.args.get("hq_model", request.form.get("hq_model", "1")) == "1"
    use_hq_model = client_hq and cfg.get("hqQualityModel", True)

    video_file = request.files["video"]
    job_id = uuid.uuid4().hex
    in_path = os.path.join(UPLOAD_DIR, f"in_{job_id}.mp4")
    out_path = os.path.join(UPLOAD_DIR, f"out_{job_id}.mp4")
    video_file.save(in_path)

    vinfo = probe_video_info(in_path)
    in_fps = vinfo["fps"]
    in_w = vinfo["width"]
    in_h = vinfo["height"]
    is_high_fps = in_fps > 60.5
    is_above_1080p = max(in_w, in_h) > 1920 or min(in_w, in_h) > 1080

    # Target FPS: lock to 60fps or 120fps max so TikTok's ingest server doesn't flag irregular VFR
    target_fps = 120 if in_fps > 90 else (60 if in_fps > 45 else 30)
    gop_size = target_fps * 2

    if use_hq_model:
        # 4K / 2K / 1080P Ultra-Sharp Bypass Mode:
        # Downscales 2K/4K inputs to exact 1080p using full-chroma Lanczos + stronger micro-contrast sharpening
        # so 4K detail is preserved inside a TikTok-compliant 1080p container that NEVER gets re-compressed!
        sharp_amount = "0.65" if is_above_1080p else "0.45"
        vf_chain = (
            f"scale='if(gt(iw,ih),1920,1080)':-2:flags=lanczos+accurate_rnd+full_chroma_int,"
            f"unsharp=5:5:{sharp_amount}:3:3:0.0,"
            f"format=yuv420p"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vf", vf_chain,
            "-r", str(target_fps),
            "-fps_mode", "cfr",
            "-c:v", "libx264",
            "-preset", "slow",
            "-tune", "film",
            "-profile:v", "high",
            "-level:v", "4.1",
            "-crf", "17",
            "-b:v", "9000k" if is_high_fps else "7800k",
            "-maxrate", "10500k" if is_high_fps else "9200k",
            "-bufsize", "21000k" if is_high_fps else "18400k",
            "-x264-params", "deblock=-1,-1:aq-mode=3:aq-strength=0.85:psy-rd=1.00,0.15:rc-lookahead=40:ref=4:bframes=3",
            "-g", str(gop_size),
            "-keyint_min", str(target_fps),
            "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-map_metadata", "-1",
            "-metadata", f"comment=xuqingxinnocent_original_hq_{job_id}",
            "-movflags", "+faststart",
            out_path
        ]
    else:
        # Standard Fast High Quality Bypass (1080p Lanczos):
        vf_chain = "scale='if(gt(iw,ih),1920,1080)':-2:flags=lanczos,unsharp=3:3:0.35:3:3:0.0,format=yuv420p"

        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vf", vf_chain,
            "-r", str(target_fps),
            "-fps_mode", "cfr",
            "-c:v", "libx264",
            "-preset", "medium",
            "-tune", "film",
            "-profile:v", "high",
            "-level:v", "4.1",
            "-crf", "18",
            "-b:v", "7500k",
            "-maxrate", "8800k",
            "-bufsize", "17600k",
            "-g", str(gop_size),
            "-keyint_min", str(target_fps),
            "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-map_metadata", "-1",
            "-metadata", f"comment=xuqingxinnocent_hq_{job_id}",
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
