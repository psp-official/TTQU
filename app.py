import os
import re
import json
import urllib.request
from datetime import datetime
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CONFIG_FILE = "/tmp/ttqu_remote_config.json"
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
    return window.__XUQING_MENTION_USER__;
  }

  setTimeout(resolveXuqingMentionProfile, 300);

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
          if (val !== null) window.__USER_PRIVACY_CHOICE__ = val;
        }
      } catch (_) {}
    }, true);

    function applyAutoWatermarkAndMentionOnSubmit(obj) {
      if (!obj || typeof obj !== 'object') return false;
      if (!window.__XUQING_WM_ACTIVE__) return false;

      const captionKey = ('text' in obj && typeof obj.text === 'string' && ('text_extra' in obj || 'video_id' in obj))
        ? 'text'
        : (('desc' in obj && typeof obj.desc === 'string' && ('text_extra' in obj || 'video_id' in obj)) ? 'desc' : null);

      if (!captionKey) return false;

      let caption = obj[captionKey] || '';
      const wmText = '#xuqingxinnocent\u2022Quality upload \u2713 @xuqingxinnocent';

      if (!Array.isArray(obj.text_extra)) {
        obj.text_extra = [];
      }

      if (!caption.includes('Quality upload')) {
        const sep = (caption.length > 0 && !caption.endsWith(' ')) ? ' ' : '';
        caption = caption + sep + wmText;
        obj[captionKey] = caption;
      }

      const hashTag = '#xuqingxinnocent';
      const hashIdx = caption.lastIndexOf(hashTag);
      if (hashIdx !== -1) {
        const hasHash = obj.text_extra.some(x => x && x.start === hashIdx);
        if (!hasHash) {
          obj.text_extra.push({
            start: hashIdx,
            end: hashIdx + hashTag.length,
            user_id: '',
            type: 1,
            hashtag_name: 'xuqingxinnocent',
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
          if (applyAutoWatermarkAndMentionOnSubmit(obj)) changed = true;
          for (const k of Object.keys(obj)) {
            if (choice !== null && (k === 'visibility_type' || k === 'privacy_level' || k === 'private_status')) {
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
        if (typeof body === 'string') body = patchUploadPayload(body);
      } catch (_) {}
      return _origSend.call(this, body);
    };
  }

  window.V7Patch = async function V7Patch(file) {
    const ab = await file.arrayBuffer();
    if (!window.__XUQING_BYPASS_ACTIVE__) return ab;
    const bytes = new Uint8Array(ab);

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
  window.addEventListener('message', function(e) {
    if (!e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'WM_SET_ACTIVE') {
      wmEnabled = !!e.data.value;
      window.__XUQING_WM_ACTIVE__ = wmEnabled;
    }
  });
})();
"""

@app.route("/", methods=["GET"])
def index():
    cfg = load_remote_config()
    return jsonify({
        "status": "online",
        "server": "xuqingxinnocent Extension Backend Server",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })

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

@app.route("/api/checkup", methods=["GET", "POST"])
def api_checkup():
    return jsonify(load_remote_config())

@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    cfg = load_remote_config()
    return jsonify({
        "enableV7Patch": cfg.get("enableV7Patch", True),
        "hqQualityModel": cfg.get("hqQualityModel", True),
        "blocked": cfg.get("blocked", False)
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
