// Verified Secure badge builder: two background tabs, download, embed code and copy.
(() => {
  'use strict';
  const FILES = {light: 'nyte-verified-secure-black.svg', dark: 'nyte-verified-secure-white.svg'};
  const tabs = [...document.querySelectorAll('.bv3-tabs [role="tab"]')];
  const panel = document.getElementById('preview');
  const preview = document.getElementById('badge-preview');
  const code = document.getElementById('embed-code');
  const status = document.getElementById('copy-status');
  if (!tabs.length || !panel || !preview || !code || !status) return;
  const escape = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const value = text => `<span class="s">${escape(`"${text}"`)}</span>`;
  function select(tab, focus) {
    const theme = tab.dataset.theme;
    const path = `/assets/badges/${FILES[theme]}`;
    for (const other of tabs) {
      const on = other === tab;
      other.setAttribute('aria-selected', String(on));
      other.tabIndex = on ? 0 : -1;
    }
    if (focus) tab.focus();
    panel.className = `bv3-preview ${theme}`;
    panel.setAttribute('aria-labelledby', tab.id);
    preview.src = path;
    document.getElementById('download').href = path;
    code.innerHTML = `${escape('<a href=')}${value('https://nytokenexchange.com/badge')}\n`
      + `   aria-label=${value('NYTE Verified Secure: requirements and evidence')}${escape('>')}\n`
      + `  ${escape('<img src=')}${value(`https://nytokenexchange.com${path}`)}\n`
      + `       alt=${value('NYTE Verified Secure')} height=${value('64')}${escape('>')}\n`
      + escape('</a>');
    status.textContent = '';
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab, false));
    tab.addEventListener('keydown', event => {
      const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
      let next = null;
      if (step) next = tabs[(index + step + tabs.length) % tabs.length];
      else if (event.key === 'Home') next = tabs[0];
      else if (event.key === 'End') next = tabs[tabs.length - 1];
      if (!next) return;
      event.preventDefault();
      select(next, true);
    });
  });
  document.getElementById('copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(code.textContent);
      status.textContent = 'HTML copied. Paste it into your website.';
    } catch {
      const range = document.createRange();
      range.selectNodeContents(code);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = 'Copy unavailable here. The HTML is selected for you to copy manually.';
    }
  });
})();
