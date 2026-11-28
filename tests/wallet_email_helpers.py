"""Capture the test mailbox separately from the wallet's browser."""

import re
from html import unescape

from trusted_router.services.email import EmailMessage


class WalletMailbox:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> bool:
        self.sent.append(message)
        return True

    def confirmation(self, page: str) -> dict[str, str]:
        fields = {name: unescape(value) for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]+)"', page
        )}
        message = self.sent[-1]
        assert message.to == fields["email"]
        match = re.search(r"code is ([0-9]{8})\.", message.text_body)
        assert match is not None
        assert "http" not in message.text_body
        assert fields["challenge"] not in message.text_body
        assert match[1] not in re.sub(r'<input type="hidden"[^>]*>', "", page)
        assert 'name="code" value=' not in page
        return {**fields, "code": match[1]}
