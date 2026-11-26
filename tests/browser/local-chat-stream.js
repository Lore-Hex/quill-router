// Playwright cannot rewrite HTTPS to HTTP with route.continue. Bridge only
// the expected gateway request to loopback for tests that need a *live* SSE
// response (route.fulfill buffers its body). Leave all other fetches alone.
async function attachLocalChatStream(page, server) {
  await page.route("**/__local-chat-stream", (route) =>
    route.continue({ url: `http://127.0.0.1:${server.address().port}/stream` }));
  await page.evaluate(() => {
    const fetch = window.fetch.bind(window);
    window.fetch = (input, options) => {
      if (input !== "https://api.trustedrouter.com/v1/chat/completions") return fetch(input, options);
      if (options.credentials !== "omit" || options.redirect !== "error") {
        throw new Error("Unexpected gateway request options");
      }
      return fetch("/__local-chat-stream", options);
    };
  });
}

module.exports = { attachLocalChatStream };
