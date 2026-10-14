(function() {
  'use strict';

  let cfg = {
    server_url: '',
    api_key: '',
    opt_mode: 'fast',
    privacy_mode: 'keep',
    hq_bypass: true
  };

  chrome.storage.local.get(['server_url', 'api_key', 'opt_mode', 'privacy_mode', 'hq_bypass'], (res) => {
    cfg.server_url = res.server_url || '';
    cfg.api_key = res.api_key || '';
    cfg.opt_mode = res.opt_mode || 'fast';
    cfg.privacy_mode = res.privacy_mode || 'keep';
    cfg.hq_bypass = res.hq_bypass !== false;
    injectMainScript(cfg);
  });

  chrome.storage.onChanged.addListener((changes) => {
    for (let k of Object.keys(changes)) {
      cfg[k] = changes[k].newValue;
    }
    window.postMessage({ type: 'MY_SELF_HOSTED_CFG', config: cfg }, '*');
  });

  // Intercept file selection on TikTok Studio & process via User's Self-Hosted Server
  let isProcessingFile = false;

  async function processVideoViaServer(file, inputEl) {
    if (!cfg.server_url || cfg.opt_mode === 'off') return;
    isProcessingFile = true;
    showStatusBanner('⚡ ကိုယ်ပိုင် Server ဖြင့် ဗီဒီယိုအရည်အသွေး မြှင့်တင်နေပါသည်... ခဏစောင့်ပါ...', '#25F4EE');

    try {
      const formData = new FormData();
      formData.append('video', file);
      formData.append('mode', cfg.opt_mode);
      if (cfg.api_key) formData.append('api_key', cfg.api_key);

      const response = await fetch(cfg.server_url + '/api/optimize', {
        method: 'POST',
        body: formData
      });

      if (!response.ok) {
        throw new Error('Server status ' + response.status);
      }

      const blob = await response.blob();
      const hqFile = new File([blob], file.name, { type: 'video/mp4' });

      const dt = new DataTransfer();
      dt.items.add(hqFile);

      const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'files');
      if (desc && desc.set) {
        desc.set.call(inputEl, dt.files);
      } else {
        Object.defineProperty(inputEl, 'files', { value: dt.files, configurable: true });
      }

      inputEl.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
      inputEl.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));

      showStatusBanner('✅ HQ Optimization ပြီးဆုံးပါပြီ! Privacy ရွေးချယ်ပြီး Post တင်နိုင်ပါပြီ။', '#00e676', 4000);
    } catch (err) {
      console.error('[My TikTok HQ] Server optimization failed, using original file:', err);
      showStatusBanner('⚠️ Server ချိတ်ဆက်မှု မအောင်မြင်ပါ၊ မူရင်းဖိုင်ဖြင့်သာ ဆက်တင်ပါမည်။', '#ff5252', 4500);
      // Re-dispatch original file
      const dt = new DataTransfer();
      dt.items.add(file);
      const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'files');
      if (desc && desc.set) desc.set.call(inputEl, dt.files);
      inputEl.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
    } finally {
      setTimeout(() => { isProcessingFile = false; }, 500);
    }
  }

  document.addEventListener('change', async (e) => {
    if (isProcessingFile) return;
    if (!cfg.server_url || cfg.opt_mode === 'off') return;

    const target = e.target;
    if (!target || target.tagName !== 'INPUT' || target.type !== 'file') return;
    const file = target.files && target.files[0];
    if (!file || !file.type.startsWith('video/')) return;

    e.stopImmediatePropagation();
    await processVideoViaServer(file, target);
  }, true);

  function showStatusBanner(text, color, autoHideMs) {
    let banner = document.getElementById('my-tt-hq-banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'my-tt-hq-banner';
      banner.style.cssText = 'position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:2147483647;background:#14161d;color:#fff;padding:10px 20px;border-radius:999px;font-family:sans-serif;font-size:13px;font-weight:600;box-shadow:0 8px 25px rgba(0,0,0,0.5);border:1.5px solid ' + color + ';';
      (document.body || document.documentElement).appendChild(banner);
    }
    banner.style.borderColor = color;
    banner.textContent = text;
    if (autoHideMs) {
      setTimeout(() => { if (banner) banner.remove(); }, autoHideMs);
    }
  }

  function injectMainScript(initialConfig) {
    const s = document.createElement('script');
    s.textContent = `
      (function() {
        if (window.__MY_SELF_HOSTED_MAIN__) return;
        window.__MY_SELF_HOSTED_MAIN__ = true;

        let cfg = ${JSON.stringify(initialConfig)};
        let userClickedPrivacy = null;

        window.addEventListener('message', (e) => {
          if (e.data && e.data.type === 'MY_SELF_HOSTED_CFG') {
            cfg = e.data.config;
          }
        });

        // 1. Block Browser-Side VideoEncoder Compression
        if (window.VideoEncoder) {
          const origSupported = window.VideoEncoder.isConfigSupported;
          window.VideoEncoder.isConfigSupported = async function(c) {
            if (cfg.hq_bypass) return { supported: false };
            return origSupported.call(this, c);
          };
        }

        // 2. Track Privacy Selection in UI
        function parsePrivacy(txt) {
          if (!txt) return null;
          txt = txt.trim().toLowerCase();
          if (txt === 'only you' || txt === 'private') return 2;
          if (txt === 'friends') return 1;
          if (txt === 'everyone' || txt === 'public') return 0;
          return null;
        }

        document.addEventListener('click', (e) => {
          const el = e.target.closest('[role="option"], [data-e2e*="privacy"], li, div, span');
          if (el) {
            const p = parsePrivacy(el.innerText || el.textContent);
            if (p !== null) userClickedPrivacy = p;
          }
        }, true);

        // 3. Enforce Privacy in Upload API Payload
        function patchPayload(bodyStr) {
          try {
            if (typeof bodyStr !== 'string' || !bodyStr.trim().startsWith('{')) return bodyStr;
            let targetPrivacy = cfg.privacy_mode !== 'keep' ? parseInt(cfg.privacy_mode, 10) : userClickedPrivacy;
            if (targetPrivacy === null || isNaN(targetPrivacy)) return bodyStr;

            let data = JSON.parse(bodyStr);
            let changed = false;

            function walk(obj) {
              if (!obj || typeof obj !== 'object') return;
              for (const k of Object.keys(obj)) {
                if (k === 'visibility_type' || k === 'privacy_level' || k === 'private_status') {
                  obj[k] = targetPrivacy;
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

        const origFetch = window.fetch;
        window.fetch = async function(input, init) {
          try {
            if (init && typeof init.body === 'string') {
              init.body = patchPayload(init.body);
            }
          } catch (_) {}
          return origFetch.apply(this, arguments);
        };

        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.send = function(body) {
          try {
            if (typeof body === 'string') {
              body = patchPayload(body);
            }
          } catch (_) {}
          return origSend.call(this, body);
        };
      })();
    `;
    (document.head || document.documentElement).appendChild(s);
    s.remove();
  }
})();