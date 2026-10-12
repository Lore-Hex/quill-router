(() => {
  'use strict';
  const preview = document.getElementById('badge-preview');
  const code = document.getElementById('embed-code');
  const status = document.getElementById('copy-status');
  function update() {
    const theme = document.querySelector('input[name="theme"]:checked').value;
    const path = `/assets/badges/nyte-enterprise-${theme}.svg`;
    preview.src = path;
    preview.alt = 'NYTE Privacy Secured — Confidential AI';
    document.getElementById('preview').className = `preview ${theme}`;
    document.getElementById('download').href = path;
    code.textContent = `<a href="https://nytokenexchange.com/badge" aria-label="NYTE Privacy Secured: requirements and evidence">\n  <img src="https://nytokenexchange.com${path}"\n       alt="NYTE Privacy Secured — Confidential AI"\n       width="320" height="88" loading="lazy" style="max-width:100%;height:auto">\n</a>`;
    status.textContent = '';
  }
  document.querySelectorAll('input[type="radio"]').forEach(input => input.addEventListener('change', update));
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
