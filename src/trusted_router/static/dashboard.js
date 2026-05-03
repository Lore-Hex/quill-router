"use strict";
/* TrustedRouter marketing page — sign-in modal + MetaMask SIWE flow.
 *
 * No Alpine. Plain DOM. The dashboard is now thin marketing only; the
 * console lives at /console/* as server-rendered pages.
 *
 * Responsibilities:
 *   1. Open/close the <dialog id="signinModal"> on Sign-in clicks.
 *   2. Auto-open the modal when the URL carries `?reason=signin` (set by
 *      the console redirect when there's no session cookie).
 *   3. Drive the MetaMask SIWE handshake against /v1/auth/wallet/*.
 */
function moneyFromMicrodollars(value) {
    if (value === null || value === undefined || value === "")
        return "$0.00";
    const raw = typeof value === "number" ? String(Math.trunc(value)) : String(value);
    let micros = BigInt(raw);
    const negative = micros < 0n;
    if (negative)
        micros = -micros;
    const whole = micros / 1000000n;
    const fraction = micros % 1000000n;
    if (fraction === 0n)
        return (negative ? "-$" : "$") + whole.toString() + ".00";
    const frac = fraction.toString().padStart(6, "0").replace(/0+$/, "");
    return (negative ? "-$" : "$") + whole.toString() + "." + frac;
}
function openSigninModal() {
    const dialog = document.getElementById("signinModal");
    if (!dialog)
        return;
    if (typeof dialog.showModal === "function" && !dialog.open) {
        dialog.showModal();
    }
}
function setSigninError(message) {
    const el = document.getElementById("signinError");
    if (!el)
        return;
    if (message) {
        el.textContent = message;
        el.removeAttribute("hidden");
    }
    else {
        el.textContent = "";
        el.setAttribute("hidden", "");
    }
}
async function startMetaMaskSignin() {
    setSigninError("");
    const ethereum = window.ethereum;
    if (!ethereum) {
        setSigninError("MetaMask is not installed in this browser. Try Google or GitHub.");
        return;
    }
    let address;
    try {
        const accounts = (await ethereum.request({ method: "eth_requestAccounts" }));
        if (!Array.isArray(accounts) || !accounts[0]) {
            setSigninError("No wallet account was returned. Try again.");
            return;
        }
        address = accounts[0];
    }
    catch {
        setSigninError("Wallet connection was rejected.");
        return;
    }
    const challenge = await postJSON("/v1/auth/wallet/challenge", { address });
    if (!challenge?.data?.message) {
        setSigninError("Unable to start sign-in. Please try again.");
        return;
    }
    let signature;
    try {
        signature = (await ethereum.request({
            method: "personal_sign",
            params: [challenge.data.message, address],
        }));
    }
    catch {
        setSigninError("Sign-in was cancelled.");
        return;
    }
    const verify = await postJSON("/v1/auth/wallet/verify", {
        address,
        signature,
        nonce: challenge.data.nonce,
    });
    if (!verify?.data?.redirect) {
        setSigninError("Verification failed. The nonce may have expired.");
        return;
    }
    location.href = verify.data.redirect;
}
async function postJSON(path, body) {
    try {
        const res = await fetch(path, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
            credentials: "same-origin",
        });
        if (!res.ok)
            return null;
        return (await res.json());
    }
    catch {
        return null;
    }
}
function init() {
    document.addEventListener("click", (event) => {
        const target = event.target;
        if (!target)
            return;
        const opener = target.closest('[data-action="open-signin"]');
        if (opener) {
            event.preventDefault();
            openSigninModal();
            return;
        }
        const metamask = target.closest('[data-action="metamask-signin"]');
        if (metamask) {
            event.preventDefault();
            void startMetaMaskSignin();
        }
    });
    if (location.search.includes("reason=signin")) {
        openSigninModal();
    }
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
}
else {
    init();
}
window.moneyFromMicrodollars = moneyFromMicrodollars;
