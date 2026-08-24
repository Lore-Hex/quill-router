"""What to tell someone whose identity check did not pass.

Veriff draws a line here and we follow it.

For ``resubmission_requested`` they ask integrators to say what went wrong:
"it is strongly advised that you inform the end-user about the reasons for the
verification failure and provide suggestions on how to improve their next
attempt." Those reasons are quality problems — glare, a cropped document, a
face that is not visible — and telling someone costs nothing and saves the
attempt.

For ``declined`` they say to investigate the session and, if you choose to
give the person another try, create a new one. They publish no end-user
guidance for the granular reasons, and the fraud family is why: 503 attempted
deceit, 504 device screen used, 505 printout used, 515-518 the screen-used
variants, 526 photos streamed. Printing one of those verbatim tells whoever
tripped it exactly which check fired and what to change next time.

So a decline gets the SAME neutral text no matter which signal fired, plus the
checklist that fixes the honest version of the failure — someone who
photographed a scan of their own passport reads "use the physical document"
and succeeds on the retry without ever learning which detector caught them.
The specific reason is kept for operators.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Reason codes whose text Veriff intends the end user to see. These arrive
#: with `resubmission_requested`, where being specific is the whole point.
_RESUBMISSION_ADVICE: dict[int, str] = {
    201: "The document in the photo was not fully inside the frame. Fit all four corners in.",
    202: "Your face was not clearly visible. Face a window or a lamp and remove hats or sunglasses.",
    203: "The photo was too dark or too bright. Move somewhere evenly lit.",
    204: "The document photo was blurry. Hold still and let the camera focus.",
    205: "There was glare on the document. Tilt it away from the light.",
    206: "Part of the document was covered. Keep fingers off the printed area.",
    207: "The document has expired. Use a current one.",
    208: "The back of the document is needed too. Photograph both sides.",
    209: "The document photo was too small to read. Move closer.",
    210: "The face photo and the document photo did not clearly match. Retake both in good light.",
}

_RESUBMISSION_FALLBACK = (
    "Something in the photos was not clear enough to read. Retake them in good, "
    "even light with the whole document in frame."
)

#: One neutral message for every decline, whatever the granular reason. The
#: checklist repairs the common honest mistake without naming the signal.
_DECLINED_MESSAGE = (
    "We could not verify your identity from that attempt. If you try again, "
    "hold the physical document in front of the camera — not a photo of it, "
    "a phone or laptop screen, or a printout — in even light with no glare, "
    "and make sure the document is current."
)

_EXPIRED_MESSAGE = (
    "That verification session expired before it was finished. Starting a new "
    "one takes a couple of minutes."
)


@dataclass(frozen=True)
class IdentityGuidance:
    """Copy for the verification page, and whether a retry is the next step."""

    headline: str
    detail: str
    can_retry: bool
    #: True only when the text came from Veriff's own resubmission advice.
    reason_shown: bool = False


def guidance_for(
    status: str,
    *,
    reason_code: int | None = None,
) -> IdentityGuidance | None:
    """Return what to show, or None when there is nothing to say yet."""
    normalized = (status or "").strip().lower()
    if normalized == "resubmission_requested":
        detail = _RESUBMISSION_ADVICE.get(int(reason_code)) if reason_code else None
        return IdentityGuidance(
            headline="Veriff needs another photo",
            detail=detail or _RESUBMISSION_FALLBACK,
            can_retry=True,
            reason_shown=detail is not None,
        )
    if normalized == "declined":
        # Deliberately ignores reason_code. See the module docstring.
        return IdentityGuidance(
            headline="That attempt did not pass",
            detail=_DECLINED_MESSAGE,
            can_retry=True,
        )
    if normalized == "expired":
        return IdentityGuidance(
            headline="The session expired",
            detail=_EXPIRED_MESSAGE,
            can_retry=True,
        )
    return None
