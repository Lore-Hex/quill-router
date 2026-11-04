"""Retired envelope labels retained for historical records and frozen oracles.

There are deliberately no credential writers or live owner-endpoint readers.
"""

USER_MODEL_SECRET_NAMESPACE = "user_model"  # noqa: S105 - historical namespace, not a credential
USER_MODEL_ENDPOINT_KEY_PURPOSE = "user_model_endpoint_key"
USER_MODEL_SIGNING_PURPOSE = "user_model_signing"
