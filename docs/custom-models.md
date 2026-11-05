# Retired: custom and user-provided models

Custom prompt-wrapper models and user-provided inference endpoints have been
removed from the console, public catalog, management API, and enclave runtime.
Requests using their old model IDs, including fallback lists, are rejected.
Use models from the managed catalog instead.

Existing financial records and settlement of previously admitted requests are
preserved. Previously earned funds remain accessible through `/console/earnings`
and `/v1/earnings`; `/v1/earnings/transfer` transfers earnings to account credits.
The legacy earnings-only API aliases remain for compatibility. They cannot
register, probe, or invoke a model.
