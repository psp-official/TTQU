document.addEventListener('DOMContentLoaded', () => {
  const serverUrl = document.getElementById('serverUrl');
  const apiKey = document.getElementById('apiKey');
  const optMode = document.getElementById('optMode');
  const privacyMode = document.getElementById('privacyMode');
  const hqToggle = document.getElementById('hqToggle');
  const saveBtn = document.getElementById('saveBtn');
  const saveMsg = document.getElementById('saveMsg');

  chrome.storage.local.get(['server_url', 'api_key', 'opt_mode', 'privacy_mode', 'hq_bypass'], (res) => {
    serverUrl.value = res.server_url || '';
    apiKey.value = res.api_key || '';
    optMode.value = res.opt_mode || 'fast';
    privacyMode.value = res.privacy_mode || 'keep';
    hqToggle.checked = res.hq_bypass !== false;
  });

  saveBtn.addEventListener('click', () => {
    const cleanUrl = serverUrl.value.trim().replace(/\/+$/, '');
    chrome.storage.local.set({
      server_url: cleanUrl,
      api_key: apiKey.value.trim(),
      opt_mode: optMode.value,
      privacy_mode: privacyMode.value,
      hq_bypass: hqToggle.checked
    }, () => {
      saveMsg.textContent = '✅ သိမ်းဆည်းပြီးပါပြီ!';
      setTimeout(() => { saveMsg.textContent = ''; }, 2000);
    });
  });
});
