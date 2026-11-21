// Keep the mailbox out of the initial DOM, including for crawlers that render JavaScript.
for (const link of document.querySelectorAll("a[data-provider-contact]")) {
  const prepare = () => {
    const address = atob("cHJvdmlkZXIub3BzQG55dGV4LmNvbQ==");
    const subject = link.dataset.providerContact === "application"
      ? "?subject=" + encodeURIComponent("Provider marketplace application for [company]")
      : "";
    link.href = "mailto:" + address + subject;
  };
  for (const event of ["click", "auxclick", "contextmenu"]) {
    link.addEventListener(event, prepare, { once: true });
  }
}
