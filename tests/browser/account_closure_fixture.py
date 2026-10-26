"""Render account closure states from an isolated, synthetic test account."""
import json

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE


def render() -> dict[str, str]:
    app = create_app(Settings(environment="test", storage_backend="memory", sentry_dsn=None), init_observability=False)
    with TestClient(app) as client:
        user = STORE.ensure_user("closure-preview@example.com", trial_credit_microdollars=0)
        raw, _ = STORE.create_auth_session(user_id=user.id, provider="github", label="Preview", ttl_seconds=3600)
        client.cookies.set("tr_session", raw)
        preferences = client.get("/console/account/preferences")
        confirmation = client.get("/console/account/delete")
        assert preferences.status_code == confirmation.status_code == 200
        token = BeautifulSoup(confirmation.text, "html.parser").select_one('input[name="csrf_token"]')["value"]
        final = client.post("/console/account/delete", headers={"Origin": "http://testserver"},
                             data={"csrf_token": token, "confirmation": "DELETE", "acknowledge": "yes"})
        assert final.status_code == 200 and not STORE.get_user(user.id).disabled
        final_token = BeautifulSoup(final.text, "html.parser").select_one('input[name="final_token"]')["value"]
        closed = client.post("/console/account/delete/confirm", headers={"Origin": "http://testserver"},
                             data={"csrf_token": token, "final_token": final_token, "confirm_close": "yes"})
        assert closed.status_code == 200
        return {"preferences": preferences.text, "confirmation": confirmation.text, "final": final.text, "closed": closed.text}


if __name__ == "__main__":
    print(json.dumps(render()))
