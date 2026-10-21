/* One-shot key setup. Keys and request content stay in this page's memory. */
(() => {
  'use strict';
  const root = document.querySelector('[data-key-onboarding]');
  if (!root) return;
  const find = id => root.querySelector(`#${id}`);
  const key = find('onboarding-key').textContent.trim();
  const base = root.dataset.apiBase.replace(/\/$/, '');
  const agentPrompt = find('setup-code').textContent;
  async function copy(button, text, status) {
    try {
      await copyText(text);
      button.querySelector('span').textContent = 'Copied';
      status.textContent = 'Copied to clipboard';
      status.classList.remove('ux-error');
      return true;
    } catch {
      status.textContent = 'Select the text to copy it manually.';
      status.classList.add('ux-error');
      return false;
    }
  }
  find('save-key').addEventListener('click', async () => {
    if (await copy(find('save-key'), key, find('key-status'))) {
      find('save-key').classList.remove('primary');
      find('copy-setup').classList.add('primary');
      find('run').classList.add('primary');
    }
  });
  const snippets = {
    'Coding agent': agentPrompt,
    Python: `from openai import OpenAI\n\nclient = OpenAI(\n    api_key=${JSON.stringify(key)},\n    base_url=${JSON.stringify(base)}\n)\n\nresponse = client.chat.completions.create(\n    model="trustedrouter/cheap",\n    messages=[{"role": "user", "content": "Hello!"}]\n)\nprint(response.choices[0].message.content)`,
    JavaScript: `import OpenAI from "openai";\n\nconst client = new OpenAI({\n  apiKey: ${JSON.stringify(key)},\n  baseURL: ${JSON.stringify(base)}\n});\n\nconst reply = await client.chat.completions.create({\n  model: "trustedrouter/cheap",\n  messages: [{role: "user", content: "Hello!"}]\n});\nconsole.log(reply.choices[0].message.content);`,
    cURL: `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"trustedrouter/cheap","messages":[{"role":"user","content":"Hello!"}]}'`,
  };
  find('method').addEventListener('change', () => {
    const method = find('method').value;
    find('setup-code').textContent = snippets[method];
    find('copy-setup').querySelector('span').textContent = method === 'Coding agent' ? 'Copy agent prompt' : 'Copy code';
  });
  find('copy-setup').addEventListener('click', () => copy(find('copy-setup'), find('setup-code').textContent, find('setup-status')));
  function track(event, attempt, details = {}) {
    // Metadata only: never send the key, prompt, output, or provider error text.
    fetch('/analytics/events', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event, attempt_id: attempt, ...details}),
    }).catch(() => {});
  }
  find('run').addEventListener('click', async () => {
    if (find('run').disabled) return;
    const prompt = find('prompt').value.trim();
    if (!prompt) { find('prompt').focus(); return; }
    const attempt = crypto.randomUUID();
    const started = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    const output = find('result').querySelector('p');
    let status = 0;
    let failure = 'network_error';
    find('run').disabled = true;
    find('run').querySelector('span').textContent = 'Sending…';
    find('result').hidden = true;
    find('connected').hidden = true;
    track('onboarding_call_started', attempt);
    try {
      const response = await fetch(`${base}/chat/completions`, {
        method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
        signal: controller.signal,
        headers: {'Authorization': `Bearer ${key}`, 'Content-Type': 'application/json'},
        body: JSON.stringify({model: 'trustedrouter/cheap', messages: [{role: 'user', content: prompt}], max_tokens: 256, stream: false}),
      });
      status = response.status;
      if (!response.ok) { failure = 'http_error'; throw new Error('http_error'); }
      failure = 'invalid_response';
      const body = await response.json();
      const text = body.choices?.[0]?.message?.content;
      if (typeof text !== 'string' || !text.trim()) { failure = 'empty_output'; throw new Error('empty_output'); }
      output.textContent = text;
      find('connected').hidden = false;
      track('onboarding_call_succeeded', attempt, {http_status: status, elapsed_ms: Math.min(120000, Math.round(performance.now() - started))});
    } catch {
      if (controller.signal.aborted) failure = 'timeout';
      if (status === 402) {
        output.textContent = 'Not enough credit. ';
        const link = document.createElement('a');
        link.href = '/console/credits';
        link.textContent = 'Add credits to try again.';
        output.append(link);
      } else output.textContent = status === 401 || status === 403 ? 'This key could not authorize the request. Check its status in API Keys.'
          : failure === 'timeout' ? 'The request timed out. It may still finish and use credit. Check usage before retrying.'
            : 'The request did not return a response. Check usage before retrying.';
      track('onboarding_call_failed', attempt, {http_status: status, elapsed_ms: Math.min(120000, Math.round(performance.now() - started)), failure_reason: failure});
    } finally {
      clearTimeout(timer);
      find('result').hidden = false;
      find('run').disabled = false;
      find('run').querySelector('span').textContent = 'Send request';
    }
  });
})();
