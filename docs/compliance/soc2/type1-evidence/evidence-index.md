# SOC 2 Type I Evidence Index

Review date: 2026-06-07

| Evidence ID | Artifact | Status | Controls | Notes |
|---|---|---|---|---|
| E-001 | `docs/compliance/soc2/README.md` | Collected | TR-POL-001, TR-AUDIT-001 | Readiness binder overview. |
| E-002 | `docs/compliance/soc2/system-description.md` | Collected | TR-GOV-001, TR-DATA-001, TR-PRIV-001 | System description draft. Needs management approval. |
| E-003 | `docs/compliance/soc2/control-matrix.md` | Collected | All controls | Draft criteria mapping. Needs auditor confirmation. |
| E-004 | `docs/compliance/soc2/policies/` | Collected | Policy controls | Policy set exists. Needs CEO approval record. |
| E-005 | `docs/compliance/soc2/type1-evidence/cli-snapshot-2026-06-07.md` | Collected | TR-ACCESS-001, TR-ACCESS-004, TR-CHANGE-001, TR-OPS-001, TR-AVAIL-002 | Non-secret CLI snapshot. |
| E-006 | `docs/compliance/soc2/type1-evidence/risk-register-2026-06-07.md` | Collected | TR-RISK-001, TR-RISK-002 | Dated internal risk register. |
| E-007 | `docs/compliance/soc2/type1-evidence/access-review-2026-06-07.md` | Draft | TR-ACCESS-001, TR-ACCESS-002, TR-ACCESS-003, TR-ACCESS-004 | Needs owner review/sign-off and external admin exports. |
| E-008 | `docs/compliance/soc2/type1-evidence/vendor-review-2026-06-07.md` | Draft | TR-VENDOR-001, TR-VENDOR-002 | Needs vendor account/contract status attachments. |
| E-009 | `docs/compliance/soc2/type1-evidence/asset-inventory-2026-06-07.md` | Draft | TR-ACCESS-001, TR-OPS-001, TR-AVAIL-002 | Based on code and CLI snapshot. Needs production owner approval. |
| E-010 | `docs/compliance/soc2/type1-evidence/change-record-2026-06-07.md` | Draft | TR-CHANGE-001, TR-CHANGE-002 | Recent deploy evidence captured. Branch protection gap remains. |
| E-011 | `docs/compliance/soc2/type1-evidence/incident-log-2026-06-07.md` | Draft | TR-OPS-002, TR-OPS-003 | Includes smoke-test failures and legal route 404 as events needing triage. |
| E-012 | `docs/compliance/soc2/type1-evidence/vulnerability-review-2026-06-07.md` | Draft | TR-VULN-001, TR-VULN-002 | Needs scan exports. |
| E-013 | Local test run `uv run pytest -q` | Collected | TR-DATA-002, TR-PRIV-002, TR-PI-001, TR-PI-002 | 799 passed, 31 skipped on 2026-06-07. |
| E-014 | Focused test run `tests/test_public_seo_pages.py tests/test_stubs_and_security.py tests/test_gateway_fallback_billing.py tests/test_core_api.py` | Collected | TR-DATA-002, TR-PI-001, TR-PI-002 | 65 passed on 2026-06-07. |
| E-015 | Production HTTP snapshot | Collected with exception | TR-POL-001, TR-PRIV-001 | `/`, `/security`, `/providers`, trust, status are live; `/legal` is 404 until deploy. |
| E-016 | Management assertion draft | Draft | TR-AUDIT-001 | Must be reviewed and signed by Joseph Perla, CEO. |
| E-017 | Open evidence gaps | Collected | All controls | Gap register for pre-auditor remediation. |

