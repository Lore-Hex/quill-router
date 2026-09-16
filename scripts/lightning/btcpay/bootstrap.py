"""Private first-admin bootstrap. Run only against a loopback BTCPay listener.

Never prints passwords, API tokens, or invitation links. State is resumable and
root-only; no customer payment, wallet, channel, or LND spending API is called.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import secrets
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ORIGIN = "http://127.0.0.1:49392"
PUBLIC = "https://btcpay.lightningrouter.ai"
OWNER = "security@trustedrouter.com"
GREG = "contact@taoeffect.com"
READ_PERMISSIONS = {"btcpay.store.canviewstoresettings", "btcpay.store.canviewlightninginvoice"}


def private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refuse symlink state")
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Inputs(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.values: dict[str, str] = {}
        self.checked: set[str] = set()
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        name = data.get("name")
        if tag != "input" or not name:
            return
        if data.get("type") == "checkbox":
            if "checked" in data:
                self.checked.add(name)
            return
        self.values[name] = data.get("value") or ""


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme, target.netloc) != ("http", "127.0.0.1:49392"):
            raise RuntimeError("Refuse bootstrap credentials redirect outside loopback")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self) -> None:
        self.token = ""
        self.basic = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            LocalRedirect(),
        )

    def request(self, path: str, data: bytes | None = None, *, method: str = "GET",
                content_type: str = "application/json", authenticated: bool = True) -> tuple[int, str]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected relative API path")
        headers = {"Content-Type": content_type}
        if authenticated:
            if self.token:
                headers["Authorization"] = "token " + self.token
            elif self.basic:
                headers["Authorization"] = "Basic " + self.basic
        request = urllib.request.Request(ORIGIN + path, data=data, method=method, headers=headers)  # noqa: S310 - fixed loopback origin
        try:
            with self.opener.open(request, timeout=30) as response:  # noqa: S310 - fixed loopback origin
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def api(self, path: str, body: dict[str, Any] | None = None, *, method: str = "GET") -> Any:
        status, text = self.request(path, json.dumps(body).encode() if body is not None else None, method=method)
        if not 200 <= status < 300:
            # Response bodies may contain credentials. Only the status is safe.
            raise RuntimeError(f"BTCPay {method} {path.split('?')[0]} failed: {status}")
        return json.loads(text) if text else None

    def form(self, path: str, values: dict[str, Any]) -> str:
        status, page = self.request(path)
        if status != 200:
            raise RuntimeError("Form unavailable")
        token = Inputs(page).values.get("__RequestVerificationToken")
        if not token:
            raise RuntimeError("Form anti-forgery token missing")
        status, result = self.request(path, urllib.parse.urlencode({
            "__RequestVerificationToken": token, **values,
        }, doseq=True).encode(), method="POST", content_type="application/x-www-form-urlencoded")
        if status != 200:
            raise RuntimeError(f"Form submission failed: {status}")
        return result


def bootstrap(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text()) if path.exists() else {"owner_password": secrets.token_urlsafe(36)}
    private_json(path, state)
    if state.get("complete"):
        return {"complete": True, "store_id": state["store_id"]}
    client = Client()
    if not state.get("owner_id"):
        owner = client.api("/api/v1/users", {
            "email": OWNER, "password": state["owner_password"], "isAdministrator": True,
            "name": "Joseph Perla", "sendInvitationEmail": False,
        }, method="POST")
        state["owner_id"] = owner["id"]
        private_json(path, state)
    client.basic = base64.b64encode(f"{OWNER}:{state['owner_password']}".encode()).decode()
    if not state.get("bootstrap_api_key"):
        result = client.api("/api/v1/api-keys", {"label": "Temporary private bootstrap", "permissions": ["unrestricted"]}, method="POST")
        state["bootstrap_api_key"] = result["apiKey"]
        private_json(path, state)
    client.token = state["bootstrap_api_key"]
    client.form("/login", {"Email": OWNER, "Password": state["owner_password"], "Method": "Password"})
    policy_values = {
        "EnableRegistration": "false", "RequiresUserApproval": "true", "AllowSearchEngines": "false",
        "EnableNonAdminCreateUserApi": "false", "AllowStoreOwnersToSkipInvitation": "false",
        "AllowLightningInternalNodeForAll": "false", "AllowHotWalletForAll": "false",
        "AllowCreateColdWalletForAll": "false", "CheckForNewVersions": "true",
        "StoreQuota": "0", "DefaultRole": "Owner", "LangTranslation": "English", "command": "Save",
    }
    page = client.form("/server/policies", policy_values)
    inputs = Inputs(page)
    if inputs.values.get("StoreQuota") != "0" or "RequiresUserApproval" not in inputs.checked:
        raise RuntimeError("Server policies not saved")
    forbidden = {"EnableRegistration", "EnableNonAdminCreateUserApi", "AllowHotWalletForAll",
                 "AllowCreateColdWalletForAll", "AllowLightningInternalNodeForAll", "AllowStoreOwnersToSkipInvitation"}
    if inputs.checked & forbidden:
        raise RuntimeError("Unsafe server policy remains enabled")
    status, _ = client.request("/api/v1/users", b'{"email":"registration-check@example.invalid","password":"NeverAnActualAccount!123"}', method="POST", authenticated=False)
    if status not in {401, 403}:
        raise RuntimeError("Anonymous registration was not denied")
    if not state.get("store_id"):
        store = client.api("/api/v1/stores", {"name": "LightningRouter", "website": "https://lightningrouter.ai"}, method="POST")
        state["store_id"] = store["id"]
        private_json(path, state)
    store_path = "/api/v1/stores/" + state["store_id"]
    roles = client.api(store_path + "/roles")
    observer = next((role for role in roles if role["role"] == "Observer"), None)
    if observer is None:
        client.form("/stores/" + state["store_id"] + "/roles/create", {
            "Role": "Observer", "Permissions": sorted(READ_PERMISSIONS), "command": "Save",
        })
        observer = next((role for role in client.api(store_path + "/roles") if role["role"] == "Observer"), None)
    if not observer or set(observer["permissions"]) != READ_PERMISSIONS:
        raise RuntimeError("Observer role must have exactly the reviewed read permissions")
    connection = "type=lnd-rest;server=https://10.92.0.2:8080/;macaroonfilepath=/run/secrets/lnd.macaroon;certfilepath=/run/secrets/lnd.pem"
    client.api(store_path + "/payment-methods/BTC-LN", {"enabled": True, "config": {"connectionString": connection}}, method="PUT")
    node = client.api(store_path + "/lightning/BTC/info")
    if not node.get("nodeURIs") or not node.get("blockHeight"):
        raise RuntimeError("Lightning node info missing")
    expected_file = path.parent / "expected-node.json"
    if expected_file.exists():
        expected = json.loads(expected_file.read_text())["node_pubkey"]
        if not all(uri.split("@")[0] == expected for uri in node["nodeURIs"]):
            raise RuntimeError("BTCPay connected to an unexpected Lightning node")
    if not state.get("greg_id"):
        greg = client.api("/api/v1/users", {"email": GREG, "name": "Greg", "isAdministrator": False, "sendInvitationEmail": False}, method="POST")
        state["greg_id"] = greg["id"]
        private_json(path, state)
    users = client.api(store_path + "/users")
    if not any(user["id"] == state["greg_id"] for user in users):
        client.api(store_path + "/users", {"id": state["greg_id"], "storeRole": observer["id"], "requireInvitation": False}, method="POST")
    users = client.api(store_path + "/users")
    if not any(user["id"] == state["greg_id"] and user["roleId"] == observer["id"] for user in users):
        raise RuntimeError("Greg's role does not match Observer")
    verify_observer(client, state["store_id"], observer["id"])
    greg = client.api("/api/v1/users/" + state["greg_id"])
    invite = greg.get("invitationUrl", "")
    if not invite or not urllib.parse.urlsplit(invite).path.startswith("/invite/"):
        raise RuntimeError("Expected an account invitation")
    # The bootstrap uses loopback; never deliver an internal-origin invitation.
    url = urllib.parse.urlsplit(invite)
    state["greg_invitation"] = PUBLIC + urllib.parse.urlunsplit(("", "", url.path, url.query, ""))
    state["owner_email"] = OWNER
    state["greg_email"] = GREG
    private_json(path, state)
    client.api("/api/v1/api-keys/current", method="DELETE")
    state.pop("bootstrap_api_key", None)
    state["complete"] = True
    private_json(path, state)
    return {"complete": True, "store_id": state["store_id"], "greg_role": "Observer", "public_registration": False}


def verify_observer(admin: Client, store_id: str, role_id: str) -> None:
    # Greg's account must remain invitation-only until he sets his own password.
    # Exercise the identical store role with a temporary account, then delete it.
    user = admin.api("/api/v1/users", {
        "email": "btcpay-test-" + secrets.token_hex(6) + "@lightningrouter.ai",
        "password": secrets.token_urlsafe(36), "isAdministrator": False,
        "sendInvitationEmail": False,
    }, method="POST")
    user_id = user["id"]
    try:
        admin.api("/api/v1/stores/" + store_id + "/users", {"id": user_id, "storeRole": role_id, "requireInvitation": False}, method="POST")
        key = admin.api("/api/v1/users/" + user_id + "/api-keys", {
            "label": "Temporary least-privilege verification", "permissions": ["unrestricted"],
        }, method="POST")
        observer = Client()
        observer.token = key["apiKey"]
        stores = observer.api("/api/v1/stores")
        if {store["id"] for store in stores} != {store_id}:
            raise RuntimeError("Observer can access unexpected stores")
        observer.api("/api/v1/stores/" + store_id + "/invoices?take=1")
        for method, path, body in (
            ("GET", "/api/v1/users", None),
            ("GET", "/api/v1/stores/" + store_id + "/payment-methods?includeConfig=true", None),
            ("GET", "/api/v1/stores/" + store_id + "/lightning/BTC/channels", None),
            ("POST", "/api/v1/stores/" + store_id + "/lightning/BTC/invoices/pay", b'{}'),
        ):
            code, _ = observer.request(path, body, method=method)
            if code not in {401, 403}:
                raise RuntimeError("Observer authorization did not deny a privileged operation")
    finally:
        admin.api("/api/v1/users/" + user_id, method="DELETE")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(bootstrap(args.state)))
