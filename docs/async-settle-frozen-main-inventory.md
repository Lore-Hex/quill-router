# Frozen-main execution inventory

Observed inventory: **330 modules, 1630 distinct qualified names, 1665 execution entries**.

BASE: `8d781cf096c43cdc29aa54c37f8d3820b80cf587` plus the source changes in `worktree-pins.json`. CPython 3.12.3.

Guarded frozen setup and HTTP/drain/state-capture executions in PR G CPython 3.12.3 default-clock proof_oracle shard 1/4; negative controls excluded. This inventory is evidence, **not an allowlist**. Fixture seed preparation is excluded; module/class bodies and comprehensions are included. No observed live router call is permitted.

Archive SHA-256: `886540b447d9d779855d2d1f3843c288e722e981b65619a80b2b2687be77e345`.

[Machine-readable records](../tests/fakes/frozen_main/execution-inventory.json) · [all file pins](../tests/fakes/frozen_main/pins.json) · [regeneration instructions](../tests/fakes/frozen_main/README.md) · [guard coverage and scope](design/async-settle-outbox-v1.md#frozen-main-coverage)

`Code line` means `co_firstlineno`; generated dataclass methods use their generated code line and are attributed to the owning class and pinned source file. Standard-library/third-party machinery and the shared fake IO engine are outside this router inventory.


## `trusted_router`

Frozen alias: `frozen_main`. Member: `src/trusted_router/__init__.py`.

SHA-256: `9206e8b4cc57923ecbabbc3ff2fe233771ab71c82bd6b886bb7f4935fce9c238`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.acquisition`

Frozen alias: `frozen_main.acquisition`. Member: `src/trusted_router/acquisition.py`.

SHA-256: `86099aeb3dd401b71ce79a997a029094111b15b537fe9408f96a1a1699e5e4b0`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 52 |
| `<module>` | 1 |
| `AttributionContext` | 90 |
| `__create_fn__` | 1 |
| `_defer_usage_check` | 1020 |
| `_privacy_signal_enabled` | 890 |
| `_should_capture_request` | 842 |
| `_usage_check_due` | 1009 |
| `decode_attribution_cookie` | 197 |
| `prepare_request_attribution` | 100 |
| `record_successful_api_call` | 395 |
| `record_successful_api_call_safely` | 423 |

## `trusted_router.adapter`

Frozen alias: `frozen_main.adapter`. Member: `src/trusted_router/adapter.py`.

SHA-256: `a0693329557b83b0578d103ac14cef4f33fd03f540b9e2001c95763d157b0e76`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.ai_iq`

Frozen alias: `frozen_main.ai_iq`. Member: `src/trusted_router/ai_iq.py`.

SHA-256: `f5ee80e0d447f230ddf14e39fba9fe5c8302d13987964c58ec70049284da4a7e`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 175 |
| `<module>` | 1 |
| `AiIqCatalogPayload` | 47 |
| `AiIqModel` | 34 |
| `AiIqSpeed` | 29 |

## `trusted_router.analytics_sink`

Frozen alias: `frozen_main.analytics_sink`. Member: `src/trusted_router/analytics_sink.py`.

SHA-256: `16b5df26f815beccd2ee9912afee6d90a80f7a59320b429bca07df711df5eada`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AnalyticsSink` | 55 |
| `ClickHouseAnalyticsSink` | 83 |
| `NullAnalyticsSink` | 66 |
| `create_analytics_sink` | 278 |

## `trusted_router.app_markup_billing`

Frozen alias: `frozen_main.app_markup_billing`. Member: `src/trusted_router/app_markup_billing.py`.

SHA-256: `0f18a2449097ad23ea85dc3b2020a40ff920cf6201d87c81990ec30ad2bfa9d0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.apps`

Frozen alias: `frozen_main.apps`. Member: `src/trusted_router/apps.py`.

SHA-256: `3706f8c3cf6740db324fd9983e3aa0e0d205d36d09275cd9f17482ec94857465`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.async_settle_fence`

Frozen alias: `frozen_main.async_settle_fence`. Member: `src/trusted_router/async_settle_fence.py`.

SHA-256: `76739fea9c7eff2125982704c9a96462e21beab94e3c5ad9f1d8489db1e9b413`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `frozen_apply` | 16 |
| `frozen_apply.<locals>.apply` | 17 |

## `trusted_router.async_settle_ticket`

Frozen alias: `frozen_main.async_settle_ticket`. Member: `src/trusted_router/async_settle_ticket.py`.

SHA-256: `ed0b7102ad0a9a39620e66d69fa5e11fc8c34ff763fce6ba37dde003f9e1df90`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `TicketClaims` | 27 |
| `TicketSigner` | 94 |
| `TicketSigner.__init__` | 95 |
| `TicketSigner.__init__.<locals>.<genexpr>` | 98 |
| `verify_lookup_ticket` | 76 |

## `trusted_router.auth`

Frozen alias: `frozen_main.auth`. Member: `src/trusted_router/auth.py`.

SHA-256: `67ad9631fcfcd5323111cbe23da7d6a8a6acb9235ab7ecbec285b6f4be583ad2`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Principal` | 55 |
| `__create_fn__` | 1 |
| `get_authorization_bearer` | 83 |
| `require_scope` | 301 |
| `settings_from_request` | 280 |

## `trusted_router.axiom_config`

Frozen alias: `frozen_main.axiom_config`. Member: `src/trusted_router/axiom_config.py`.

SHA-256: `205d3395f39fa36891b593cecf782e76b7b5314d39b043006c4e9b538fcc947c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_AxiomNoiseFilter` | 417 |
| `_AxiomScrubFilter` | 348 |
| `_DroppingQueueHandler` | 473 |
| `_IdempotentQueueListener` | 450 |
| `_SafeAxiomHandler` | 553 |
| `_running_under_pytest` | 340 |
| `init_axiom` | 103 |

## `trusted_router.bedrock_group_buy`

Frozen alias: `frozen_main.bedrock_group_buy`. Member: `src/trusted_router/bedrock_group_buy.py`.

SHA-256: `8470349bb6f1ecc891eabab39be86ef5f391206e2112c4dc81c475c8c362cb38`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 39 |
| `<module>` | 1 |
| `BedrockGroupBuyPublicSnapshot` | 53 |
| `__create_fn__` | 1 |

## `trusted_router.benchmark_reports`

Frozen alias: `frozen_main.benchmark_reports`. Member: `src/trusted_router/benchmark_reports.py`.

SHA-256: `a2cfd381e60fe3c909b722d60fc0acf3df69f8f27cdbff4392298de1d8a81d5e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.benchmark_samples`

Frozen alias: `frozen_main.benchmark_samples`. Member: `src/trusted_router/benchmark_samples.py`.

SHA-256: `cd4cb63e00dfbbe2e6bfcabef4c47e7c5792e473624907acf56ab48a7eb1efda`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.benchmark_scores`

Frozen alias: `frozen_main.benchmark_scores`. Member: `src/trusted_router/benchmark_scores.py`.

SHA-256: `ea9148d3d270617ebb477ac518b03f7c42d85346bf2c71d751f6dce11747fd9b`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BenchmarkDef` | 34 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.billing_policy`

Frozen alias: `frozen_main.billing_policy`. Member: `src/trusted_router/billing_policy.py`.

SHA-256: `3ca776792e66f86ecdbadf239076fce3988dbc7bd909dc7dbbf321534e967ef1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.billing_snapshot`

Frozen alias: `frozen_main.billing_snapshot`. Member: `src/trusted_router/billing_snapshot.py`.

SHA-256: `94b15947135794bafc3e07e90cda86049d0b6d3e911cb0fa4a39e6702608b38a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AcceptanceOutcome` | 338 |
| `AcceptanceStatus` | 330 |
| `BillingSnapshot` | 256 |
| `BillingSnapshot.unique_candidates` | 266 |
| `Candidate` | 220 |
| `Candidate.supported` | 233 |
| `Eligibility` | 149 |
| `Evaluation` | 299 |
| `Frozen` | 77 |
| `Frozen.integer_versions` | 125 |
| `Frozen.model_validate` | 80 |
| `Frozen.model_validate_json` | 93 |
| `Frozen.scalar_strings` | 142 |
| `Frozen.strict_wire_path` | 115 |
| `NormalizedUsage` | 282 |
| `NormalizedUsage.consistent` | 290 |
| `Rates` | 208 |
| `RawUsage` | 274 |
| `TerminalEnvelope` | 304 |
| `TerminalEnvelope.refund_is_free` | 323 |
| `Tier` | 215 |
| `_WireCapability` | 47 |
| `_WireCapability.__init__` | 48 |
| `_WireContext` | 52 |
| `_WireContext.__init__` | 55 |
| `_authorized_wire` | 61 |
| `_unique_object` | 373 |
| `_validate_strings` | 382 |
| `_validate_strings.<locals>.<genexpr>` | 385 |
| `_validation_options` | 69 |
| `canonical_bytes` | 361 |
| `canonical_hash` | 369 |
| `checked` | 355 |
| `evaluate` | 477 |
| `evaluate.<locals>.<genexpr>` | 481 |
| `exclusion` | 181 |
| `parse_eligibility` | 432 |
| `parse_envelope` | 424 |
| `parse_snapshot` | 420 |
| `require_eligible` | 202 |
| `strict_json_loads` | 399 |
| `validate_envelope` | 520 |
| `validate_envelope.<locals>.<genexpr>` | 525 |

## `trusted_router.byok_crypto`

Frozen alias: `frozen_main.byok_crypto`. Member: `src/trusted_router/byok_crypto.py`.

SHA-256: `2d89e6e24a6fd6d04bc540cd90587a4e7e7b4215620acacc995b244388431dff`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_derive_dev_wrapping_key` | 40 |

## `trusted_router.catalog`

Frozen alias: `frozen_main.catalog`. Member: `src/trusted_router/catalog.py`.

SHA-256: `7f4665fccf731f8b6775a2558ad597e25a953a2a2355d76ab09b1da98e900fb3`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_meta_price_range` | 424 |
| `_model_max_privacy_tier` | 460 |
| `_route_provider_slugs` | 539 |
| `canonical_orchestration_model_id` | 310 |
| `effective_endpoint` | 353 |
| `endpoints_for_model` | 389 |
| `model_eu_focused_provider_available` | 589 |
| `model_eu_focused_provider_available.<locals>.<genexpr>` | 591 |
| `model_max_privacy_tier` | 486 |
| `model_open_weights` | 516 |
| `model_open_weights.<locals>.<genexpr>` | 533 |
| `model_open_weights.<locals>.<genexpr>` | 534 |
| `model_to_openrouter_shape` | 596 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 609 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 620 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 628 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 631 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 640 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 660 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 663 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 668 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 672 |
| `model_to_openrouter_shape.<locals>.<genexpr>` | 754 |
| `model_us_provider_available` | 580 |
| `model_us_provider_available.<locals>.<genexpr>` | 582 |
| `orchestration_primitive` | 306 |
| `orchestration_role` | 319 |

## `trusted_router.catalog_capabilities`

Frozen alias: `frozen_main.catalog_capabilities`. Member: `src/trusted_router/catalog_capabilities.py`.

SHA-256: `8d12100110a36edeb0a6c7cb4a60c9541b6fc4279e7b8b7874f44132c27884f0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_strings` | 61 |
| `_strings.<locals>.<genexpr>` | 64 |
| `manifest_supported_parameters` | 90 |
| `provider_extension_parameters` | 74 |
| `union_supported_parameters` | 79 |
| `union_supported_parameters.<locals>.<lambda>` | 85 |

## `trusted_router.catalog_data`

Frozen alias: `frozen_main.catalog_data`. Member: `src/trusted_router/catalog_data.py`.

SHA-256: `8c8bb1451424a9a1859afd6e49cbbd6243a88d1cd0a83c74b3342211145e47e2`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 3322 |
| `<module>` | 1 |
| `Model` | 376 |
| `Model.__init__` | 2 |
| `ModelDocumentation` | 358 |
| `ModelDocumentation.__init__` | 2 |
| `ModelDocumentation.to_dict` | 366 |
| `ModelEndpoint` | 414 |
| `ModelEndpoint.__init__` | 2 |
| `ModelEndpoint.catalog_is_current` | 443 |
| `ModelEndpoint.is_byok` | 439 |
| `ModelOrigin` | 3699 |
| `ModelProviderPrivacyOverride` | 238 |
| `ModelProviderPrivacyOverride.__init__` | 2 |
| `NamedDecisionModel` | 2425 |
| `Provider` | 25 |
| `_DecisionFallbackRoute` | 3270 |
| `_DecisionSpec` | 3276 |
| `_EmbeddingSpec` | 3354 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |
| `_utc_now` | 20 |
| `maker_provider_slug` | 4241 |
| `model_vendor_provider_slugs` | 4260 |
| `model_vendor_provider_slugs.<locals>.<genexpr>` | 4275 |
| `offers_chat` | 2517 |

## `trusted_router.catalog_energy`

Frozen alias: `frozen_main.catalog_energy`. Member: `src/trusted_router/catalog_energy.py`.

SHA-256: `a336e64d7e10608f9dba4881a2462ea9e3352923755c2830cafefaeac9d2610a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `provider_has_renewable_inference` | 9 |
| `renewable_provider_slugs` | 13 |
| `renewable_provider_slugs.<locals>.<genexpr>` | 14 |

## `trusted_router.catalog_ingest`

Frozen alias: `frozen_main.catalog_ingest`. Member: `src/trusted_router/catalog_ingest.py`.

SHA-256: `9346e3f8b713e1ed8a4f288b5e041556fd01b7234e192a9177d8656f36999a75`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_NativeEndpointCapabilities` | 740 |
| `_NativeEndpointCapabilities.__init__` | 2 |
| `__create_fn__` | 1 |
| `_api_reported_context_windows` | 783 |
| `_apply_provider_manifest_expiry` | 213 |
| `_author_provider` | 725 |
| `_authoritative_provider_model_ids` | 299 |
| `_build_endpoints` | 158 |
| `_context_window` | 736 |
| `_decision_fallback_endpoints` | 1458 |
| `_decision_models` | 1422 |
| `_embedding_manifest_cost` | 1606 |
| `_embedding_models` | 1501 |
| `_embedding_models.<locals>.<genexpr>` | 1577 |
| `_endpoint` | 127 |
| `_filter_unserved_provider_endpoints` | 1680 |
| `_filter_unserved_provider_endpoints.<locals>._keep` | 1710 |
| `_has_token_prices` | 199 |
| `_ingested_models_and_endpoints` | 812 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 891 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 892 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 895 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 904 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 915 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 950 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 955 |
| `_ingested_models_and_endpoints.<locals>.<genexpr>` | 975 |
| `_ingested_models_and_endpoints.<locals>.endpoint_capabilities` | 830 |
| `_input_only_manifest_cost` | 1613 |
| `_is_provider_deprecated_model` | 710 |
| `_modalities` | 90 |
| `_modalities.<locals>.<genexpr>` | 94 |
| `_model_documentation` | 107 |
| `_model_documentation.<locals>.<genexpr>` | 111 |
| `_native_endpoint_capabilities` | 746 |
| `_native_endpoint_capabilities.<locals>.<genexpr>` | 771 |
| `_positive_float` | 80 |
| `_provider_manifest_dark_model_ids` | 1655 |
| `_provider_manifest_dark_model_ids.<locals>.<genexpr>` | 1670 |
| `_supplemental_provider_models_and_endpoints` | 1034 |
| `report_refused_manifest_rows` | 190 |

## `trusted_router.catalog_privacy`

Frozen alias: `frozen_main.catalog_privacy`. Member: `src/trusted_router/catalog_privacy.py`.

SHA-256: `f75a1d6fa0601a6900dadc1f1079a84546f618c42e5dac92672a43d0e433928c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_endpoint_privacy_override` | 83 |
| `_model_provider_privacy_override` | 58 |
| `_model_provider_privacy_tier` | 118 |
| `endpoint_confidential_compute` | 203 |
| `endpoint_e2ee` | 210 |
| `endpoint_meets_privacy_requirement` | 231 |
| `endpoint_privacy_tier` | 135 |
| `endpoint_provider_policy` | 217 |
| `endpoint_provider_policy_url` | 224 |
| `endpoint_stores_content` | 145 |
| `endpoint_zero_data_retention` | 160 |
| `endpoint_zero_data_retention_scope` | 176 |
| `model_provider_confidential_compute` | 189 |
| `model_provider_e2ee` | 196 |
| `model_provider_policy` | 261 |
| `model_provider_policy_url` | 268 |
| `model_provider_privacy_tier` | 110 |
| `model_provider_zero_data_retention` | 254 |
| `provider_confidential_inference` | 31 |
| `provider_privacy_tier` | 43 |
| `served_by_model_vendor` | 98 |

## `trusted_router.catalog_registry`

Frozen alias: `frozen_main.catalog_registry`. Member: `src/trusted_router/catalog_registry.py`.

SHA-256: `68da176a580280967e8b31490e4210f053a0f97b46bdec65c05a671bde8d070a`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 1658 |
| `<genexpr>` | 1671 |
| `<genexpr>` | 1674 |
| `<module>` | 1 |
| `_install_deepseek_v4_pro_release_routes` | 1365 |
| `_install_deepseek_v4_pro_release_routes.<locals>.install` | 1432 |
| `_install_deepseek_v4_pro_release_routes.<locals>.install.<locals>.<genexpr>` | 1437 |
| `_install_deepseek_v4_pro_release_routes.<locals>.install.<locals>.<genexpr>` | 1438 |
| `_install_deepseek_v4_pro_release_routes.<locals>.install.<locals>.<lambda>` | 1443 |
| `_named_decision_model_with_chain_prices` | 1680 |
| `_named_decision_model_with_chain_prices.<locals>.<genexpr>` | 1708 |
| `_named_decision_model_with_chain_prices.<locals>.<genexpr>` | 1709 |
| `_settle_deepseek_v4_pro_0423_leaf` | 1489 |
| `_settle_deepseek_v4_pro_0423_leaf.<locals>.<genexpr>` | 1507 |
| `archimedes_model` | 980 |
| `named_decision_model` | 1007 |

## `trusted_router.catalog_usage_policy`

Frozen alias: `frozen_main.catalog_usage_policy`. Member: `src/trusted_router/catalog_usage_policy.py`.

SHA-256: `83561c82477e2d1bdfa65ac31cfe2c974fd6f1870f6b039ea011ad1ce1241b21`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `provider_usage_estimation_policy` | 12 |

## `trusted_router.chat_capabilities`

Frozen alias: `frozen_main.chat_capabilities`. Member: `src/trusted_router/chat_capabilities.py`.

SHA-256: `ea48605c6e2fad3be75df1f2904ba801ee97864990146f3d34caf64bf284b966`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.choose_catalog`

Frozen alias: `frozen_main.choose_catalog`. Member: `src/trusted_router/choose_catalog.py`.

SHA-256: `25b902ac1d845ea8359d854bb776d44c89df4a50695dd86bdfb2476f72e18176`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.clickhouse_endpoints`

Frozen alias: `frozen_main.clickhouse_endpoints`. Member: `src/trusted_router/clickhouse_endpoints.py`.

SHA-256: `9d3028386c1b6a8c900d0e4dc3ff2de2e892b72b59fb5362fb805439528c25f5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.client_context`

Frozen alias: `frozen_main.client_context`. Member: `src/trusted_router/client_context.py`.

SHA-256: `220d733d405bcfa865d8d699c0fb7ca5fa7e00f50bd5fc4599b8fb5459c8e5bc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `parse_client_context` | 156 |
| `parse_gateway_request_id` | 166 |

## `trusted_router.client_events_schema`

Frozen alias: `frozen_main.client_events_schema`. Member: `src/trusted_router/client_events_schema.py`.

SHA-256: `e65b032c049a144614a9f81b973ce8a0e9e6fdc81ba1dbb8b01156d26626d22a`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 31 |
| `<module>` | 1 |
| `ClientAttempt` | 121 |
| `ClientEventsBatch` | 176 |
| `ClientMinuteCounter` | 156 |
| `ClientRequestEvent` | 136 |
| `ClientSDK` | 112 |
| `_Strict` | 108 |
| `_closed_pattern` | 27 |
| `_closed_pattern.<locals>.<genexpr>` | 28 |

## `trusted_router.client_reliability`

Frozen alias: `frozen_main.client_reliability`. Member: `src/trusted_router/client_reliability.py`.

SHA-256: `9b18512c197eb9f5b2655037ae972112783972982512b09f46c1a29afcc6a7d0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.company_affiliations`

Frozen alias: `frozen_main.company_affiliations`. Member: `src/trusted_router/company_affiliations.py`.

SHA-256: `f21d6155fedb9f4fbd4bc7aa64e2492f8ebf770fdf5a15143f861f5e45adeb64`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AffiliationDirectory` | 228 |

## `trusted_router.competitor_comparisons`

Frozen alias: `frozen_main.competitor_comparisons`. Member: `src/trusted_router/competitor_comparisons.py`.

SHA-256: `d7590b05d19a1f363bf326e37f4aa8b0f5bc39a24de0dc2a5e2df49308341c3d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ComparisonSource` | 10 |
| `CompetitorComparison` | 16 |
| `CompetitorComparison.href` | 34 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |
| `_comparison` | 116 |
| `_rows` | 50 |

## `trusted_router.config`

Frozen alias: `frozen_main.config`. Member: `src/trusted_router/config.py`.

SHA-256: `c7497caa356679100faf89b86ea26e76e8edebfc3aa76e225bde3797e0716bb4`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `GatewayRegionTarget` | 108 |
| `Settings` | 368 |
| `Settings.async_settle_admission_enabled` | 1323 |
| `Settings.async_settle_requires_protection` | 1328 |
| `Settings.async_settle_shadow_workspace_ids` | 1304 |
| `Settings.drain_budget_within_lease` | 1289 |
| `Settings.parse_pilot_workspaces` | 1308 |
| `Settings.parse_pilot_workspaces.<locals>.<genexpr>` | 1310 |
| `Settings.parse_pilot_workspaces.<locals>.<genexpr>` | 1311 |
| `Settings.parse_shadow_workspaces` | 1296 |
| `Settings.parse_shadow_workspaces.<locals>.<genexpr>` | 1298 |
| `Settings.parse_shadow_workspaces.<locals>.<genexpr>` | 1299 |
| `Settings.production_is_fail_closed` | 1334 |
| `Settings.production_is_fail_closed.<locals>.<genexpr>` | 1503 |
| `Settings.production_is_fail_closed.<locals>.<genexpr>` | 1602 |
| `Settings.remediator_mode_is_known` | 1275 |
| `Settings.settings_customise_sources` | 1255 |
| `Settings.trust_qualifying_provider_set` | 2103 |
| `Settings.trust_qualifying_provider_set.<locals>.<genexpr>` | 2105 |
| `_LocalKeyFileSource` | 2223 |
| `_LocalKeyFileSource.__call__` | 2252 |
| `_LocalKeyFileSource.__init__` | 2230 |
| `_running_under_pytest` | 2256 |
| `get_settings` | 2260 |
| `parse_gateway_region_targets` | 132 |
| `parse_settlement_inbound_tokens` | 58 |

## `trusted_router.content`

Frozen alias: `frozen_main.content`. Member: `src/trusted_router/content/__init__.py`.

SHA-256: `4716c8fab43ef6cfd0c8c54c7ca7f4adca28e7f929fe24e76e34be212205ffa5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.content.blog`

Frozen alias: `frozen_main.content.blog`. Member: `src/trusted_router/content/blog.py`.

SHA-256: `8cc18c4591cbd7aa4a116140b62dd8a096237e642b05e44ae9674f3e0ffb6f7a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BlogPost` | 6 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.content.company_signin`

Frozen alias: `frozen_main.content.company_signin`. Member: `src/trusted_router/content/company_signin.py`.

SHA-256: `0be9305335e79bf6e5f7406a66047983560c36040653dd87f2ffc3c25c30f31f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.content.legal`

Frozen alias: `frozen_main.content.legal`. Member: `src/trusted_router/content/legal.py`.

SHA-256: `ac88e892440a85f916aa4959944443a9a7b5c490901c322bef91df7b03455a7b`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.content.token_index`

Frozen alias: `frozen_main.content.token_index`. Member: `src/trusted_router/content/token_index.py`.

SHA-256: `41957d0cb88fbf5b2bde82c6912523a41a8aa00f9457defc508ad6a6ca0ac3dc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.content_handling`

Frozen alias: `frozen_main.content_handling`. Member: `src/trusted_router/content_handling.py`.

SHA-256: `b1ffacfdbacff83c720adfcb1ce7151d2075742e75e04ac08a8278109845b6cc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.creator_identity`

Frozen alias: `frozen_main.creator_identity`. Member: `src/trusted_router/creator_identity.py`.

SHA-256: `badef8ccdb007bdbc1039190f27047341e8b71dc8a61d77b7ae7a4901ffcc6b5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.credit_debt`

Frozen alias: `frozen_main.credit_debt`. Member: `src/trusted_router/credit_debt.py`.

SHA-256: `25a349fde3e2720dd6ed70eb18e3f223345ff0a52f61cb023f0f7445953cc5b4`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Inflow` | 35 |
| `Squared` | 27 |
| `__create_fn__` | 1 |

## `trusted_router.credit_transfer`

Frozen alias: `frozen_main.credit_transfer`. Member: `src/trusted_router/credit_transfer.py`.

SHA-256: `ccb2425d4db917db0c40966aede82f3e0ffe09445779e9419ec3d42961686789`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `CreditTransferConflict` | 149 |
| `DestinationMismatch` | 215 |
| `TransferIdReused` | 195 |

## `trusted_router.custom_model_billing`

Frozen alias: `frozen_main.custom_model_billing`. Member: `src/trusted_router/custom_model_billing.py`.

SHA-256: `91e8f08388c2a495e04af49b9d08c7991a296b06a229f5bfa9916a977eb64c06`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.custom_model_markup_billing`

Frozen alias: `frozen_main.custom_model_markup_billing`. Member: `src/trusted_router/custom_model_markup_billing.py`.

SHA-256: `c8fc28415a7dfb09b9cd8657d5d974532f9ed15d2be158e6616294f5c3f4151d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.custom_model_rules`

Frozen alias: `frozen_main.custom_model_rules`. Member: `src/trusted_router/custom_model_rules.py`.

SHA-256: `f4679efcc6a6a0ae83f7d3eea7ecd3e8ea5e5f3283538bf6835c7a2763a943b8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.dashboard`

Frozen alias: `frozen_main.dashboard`. Member: `src/trusted_router/dashboard.py`.

SHA-256: `d6580891bf030e47b6e6999ed12ee3ec284bf4f27df26a4e3a9857997d6cd8fc`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 194 |
| `<genexpr>` | 233 |
| `<genexpr>` | 247 |
| `<module>` | 1 |
| `BlogIndexPost` | 452 |
| `OpenRouterLandingVariant` | 426 |
| `PublicPage` | 413 |
| `_EndpointProviderView` | 5229 |
| `_ModelPublisher` | 4976 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.detached_jws`

Frozen alias: `frozen_main.detached_jws`. Member: `src/trusted_router/detached_jws.py`.

SHA-256: `85615e65d8474a8ebe96ce49012bc07674a9f273e6ddfa12bc07fc2cd5c08164`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `JWSError` | 23 |
| `TrustedKey` | 27 |
| `TrustedKey.__init__` | 2 |
| `__create_fn__` | 1 |
| `_depth` | 86 |
| `_integer` | 41 |
| `_json` | 106 |
| `_json.<locals>.<lambda>` | 114 |
| `_json_bytes` | 102 |
| `_json_bytes.<locals>.<genexpr>` | 103 |
| `_object` | 51 |
| `_pairs` | 65 |
| `_parse_int` | 77 |
| `_require` | 36 |
| `_string` | 45 |
| `_string.<locals>.<genexpr>` | 46 |
| `b64decode` | 128 |
| `b64encode` | 124 |
| `canonical` | 60 |
| `verify` | 138 |

## `trusted_router.domains`

Frozen alias: `frozen_main.domains`. Member: `src/trusted_router/domains.py`.

SHA-256: `2417be0b4822ff87720428c27d9924061f1a884bc74b71f4754f57eec61130a0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_normalized_domain` | 131 |
| `configured_control_domains` | 24 |
| `is_status_hostname` | 115 |
| `is_status_hostname.<locals>.<genexpr>` | 116 |
| `is_www_hostname` | 111 |
| `is_www_hostname.<locals>.<genexpr>` | 112 |
| `request_hostname` | 43 |

## `trusted_router.enclave_regions`

Frozen alias: `frozen_main.enclave_regions`. Member: `src/trusted_router/enclave_regions.py`.

SHA-256: `06ebfa0babbf338e367f7fafcb0e5d8382163cd2ac1274d2560e2422bb4aa181`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.endpoint_identity`

Frozen alias: `frozen_main.endpoint_identity`. Member: `src/trusted_router/endpoint_identity.py`.

SHA-256: `5ee76f6b49b9e4bb34477673868be895f95e8b76a58c42aa88d2f9bca8b1f600`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Endpoint` | 107 |
| `__create_fn__` | 1 |

## `trusted_router.errors`

Frozen alias: `frozen_main.errors`. Member: `src/trusted_router/errors.py`.

SHA-256: `13e51bc2b46cdf26aa5db7356a6214eb66bf6f473aee8f3328588bfa169374d9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.gateway_boot`

Frozen alias: `frozen_main.gateway_boot`. Member: `src/trusted_router/gateway_boot.py`.

SHA-256: `b2bae2370a207b681ed9d9441a22b2408da473b452fe470629d8f1bf1ba96dd9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BootAuthHeader` | 28 |
| `__create_fn__` | 1 |

## `trusted_router.gateway_timing`

Frozen alias: `frozen_main.gateway_timing`. Member: `src/trusted_router/gateway_timing.py`.

SHA-256: `4c90ee909448d37464767ce8fb00fedde879344da31f9c1e975b08c94fcf9411`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `GatewayTiming` | 40 |
| `GatewayTiming.__init__` | 41 |
| `GatewayTiming.snapshot` | 55 |
| `GatewayTiming.switch` | 48 |
| `_scope` | 86 |
| `gateway_phase` | 73 |
| `timed_gateway_async` | 257 |
| `timed_gateway_async.<locals>.timed` | 261 |
| `timed_gateway_sync` | 231 |
| `timed_gateway_sync.<locals>.timed` | 235 |

## `trusted_router.google_ads_conversions`

Frozen alias: `frozen_main.google_ads_conversions`. Member: `src/trusted_router/google_ads_conversions.py`.

SHA-256: `4fc7c9f0e0a8bfae83d52198c88f9bfc3faa0e3c430786882a7dae416ac12ec4`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `GoogleAdsKeySettings` | 41 |
| `_GoogleAdsKeyWrapperConfig` | 46 |
| `__create_fn__` | 1 |

## `trusted_router.identity_guidance`

Frozen alias: `frozen_main.identity_guidance`. Member: `src/trusted_router/identity_guidance.py`.

SHA-256: `9fd56a5202358c0334979cc5b4d07b89a143d9877b3bc3f6ef9ea7db514f5425`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `IdentityGuidance` | 65 |
| `__create_fn__` | 1 |

## `trusted_router.image_generation`

Frozen alias: `frozen_main.image_generation`. Member: `src/trusted_router/image_generation.py`.

SHA-256: `1ba6e13ca8c58f4fb9198b730e91f6883a42533e5953589fbd766fa7d78fff7f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.key_management`

Frozen alias: `frozen_main.key_management`. Member: `src/trusted_router/key_management.py`.

SHA-256: `78ecef2e40825b87d75a964dcab67a1dffdd6c7fb3b77ffb30c62b777c8f75c5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `GcpKmsKeyWrapper` | 113 |
| `KeyAccessDenied` | 38 |
| `KeyManagementError` | 34 |
| `KeyUnavailable` | 47 |
| `KeyWrapper` | 51 |
| `KeyWrapperConfig` | 73 |
| `KeyWrapperSettings` | 64 |
| `LocalAesKeyWrapper` | 89 |
| `__create_fn__` | 1 |

## `trusted_router.main`

Frozen alias: `frozen_main.main`. Member: `src/trusted_router/main.py`.

SHA-256: `59d62bcf5c54ec6f82b46467fb9e6b77d8eac8dc15ae73e4144fb4e90b1bfbf5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_ApplicationConsoleFormatter` | 147 |
| `_configure_application_logging` | 176 |
| `_configure_application_logging.<locals>.<genexpr>` | 194 |
| `_control_plane_inference_enabled` | 880 |
| `_make_api_router` | 760 |
| `create_app` | 209 |

## `trusted_router.markdown_negotiation`

Frozen alias: `frozen_main.markdown_negotiation`. Member: `src/trusted_router/markdown_negotiation.py`.

SHA-256: `083b3eda428dab4f3aeed46c4a10c1cb333788cf69acc2b86fa53dbfc9bacc30`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `MarkdownNegotiationMiddleware` | 264 |
| `MarkdownNegotiationMiddleware.__call__` | 288 |
| `MarkdownNegotiationMiddleware.__init__` | 285 |

## `trusted_router.marketing_experiments`

Frozen alias: `frozen_main.marketing_experiments`. Member: `src/trusted_router/marketing_experiments.py`.

SHA-256: `98fad0d10848b3992a035edcfba55e35b161b30b8aeb803da9a6ea9189aa897e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Audience` | 30 |
| `CallToAction` | 57 |
| `GoogleSearchExperimentCell` | 64 |
| `Promise` | 38 |
| `Proof` | 47 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |
| `build_google_search_cells` | 313 |
| `build_google_search_cells.<locals>.<genexpr>` | 314 |

## `trusted_router.mcp_metadata`

Frozen alias: `frozen_main.mcp_metadata`. Member: `src/trusted_router/mcp_metadata.py`.

SHA-256: `359f0677a0410754243340353a9f76091163a19e3b72c895744b11404accecb0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.mcp_skills`

Frozen alias: `frozen_main.mcp_skills`. Member: `src/trusted_router/mcp_skills.py`.

SHA-256: `fb3b02ee3e8f74ccef65687793e676ec8d4d70f7c14801fdaef0245caf85607c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SkillNotFound` | 241 |
| `SkillTooLarge` | 245 |
| `SkillsRegistry` | 113 |
| `SkillsRegistry.__init__` | 2 |
| `_Cached` | 104 |
| `__create_fn__` | 1 |

## `trusted_router.measured`

Frozen alias: `frozen_main.measured`. Member: `src/trusted_router/measured.py`.

SHA-256: `206bbb4720ada65f01d9ad79812c8904a1e7949ef9ffbbb42e5c029851ef4cb6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.middleware`

Frozen alias: `frozen_main.middleware`. Member: `src/trusted_router/middleware.py`.

SHA-256: `8e48d90100152c78d68d254e726aadef7c1a9a2779b8d0fc8865a559931cc3ad`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_canonical_campaign_url` | 530 |
| `_cookie_free_public_analytics_path` | 519 |
| `_internal_auth_before_body` | 473 |
| `_log_public_page_view` | 682 |
| `_rate_limit_request` | 540 |
| `_trusted_internal_credential` | 630 |
| `register_http_middleware` | 113 |
| `register_http_middleware.<locals>.canonical_public_host_middleware` | 202 |
| `register_http_middleware.<locals>.internal_auth_before_body_middleware` | 153 |
| `register_http_middleware.<locals>.oauth_key_exchange_cors_middleware` | 254 |
| `register_http_middleware.<locals>.public_pageview_middleware` | 232 |
| `register_http_middleware.<locals>.rate_limit_middleware` | 326 |
| `register_http_middleware.<locals>.read_only_middleware` | 275 |
| `register_http_middleware.<locals>.request_id_middleware` | 177 |
| `register_http_middleware.<locals>.security_headers_middleware` | 344 |
| `register_http_middleware.<locals>.spend_window_headers_middleware` | 163 |

## `trusted_router.model_changes`

Frozen alias: `frozen_main.model_changes`. Member: `src/trusted_router/model_changes.py`.

SHA-256: `ae30e2ca266b59e81cd37388da1bf1dd2c4cd63835abc17e291d48efaa3d60e6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `active_history` | 127 |
| `last_route_changes` | 133 |
| `load_history` | 122 |
| `load_history.<locals>.<genexpr>` | 124 |
| `timestamp` | 22 |

## `trusted_router.model_regions`

Frozen alias: `frozen_main.model_regions`. Member: `src/trusted_router/model_regions.py`.

SHA-256: `9bb09d9b9121b7b19cf9ca7975f903c361a3cd56148124d65c7522b01f9f8ea3`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ModelRegion` | 160 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.money`

Frozen alias: `frozen_main.money`. Member: `src/trusted_router/money.py`.

SHA-256: `d6abdb139ed01852ff0235ce9f4ce3ea6c781a6713391f3ee318986c4a8445eb`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `dollars_to_microdollars` | 69 |
| `microdollars_per_million_tokens_to_token_decimal` | 46 |
| `microdollars_to_decimal` | 36 |
| `microdollars_to_float` | 64 |
| `money_pair` | 26 |
| `token_cost_microdollars` | 57 |

## `trusted_router.oauth_app_policy`

Frozen alias: `frozen_main.oauth_app_policy`. Member: `src/trusted_router/oauth_app_policy.py`.

SHA-256: `2a045a609d599ffac663588a4909acceabec03d1c7941973f6eb90923d1c6a41`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.oauth_provider`

Frozen alias: `frozen_main.oauth_provider`. Member: `src/trusted_router/oauth_provider.py`.

SHA-256: `bce1e4ba95f39ba5f24d4b88c3a77ea9c5b4c21043db0f9682f8b107ac4d2ef1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `OAuthProvider` | 30 |
| `OAuthUserInfo` | 20 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.og`

Frozen alias: `frozen_main.og`. Member: `src/trusted_router/og.py`.

SHA-256: `ef44e53648aba05398354b8039113e3179a0a9f505aead4f298029a83f735629`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.openai_service_tiers`

Frozen alias: `frozen_main.openai_service_tiers`. Member: `src/trusted_router/openai_service_tiers.py`.

SHA-256: `28928c57283a77ce19fbaed1e3bebb2acfbccea0ff26dcb0ab26275b97195cdf`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `OpenAIPriorityPricing` | 20 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |
| `_customer_priority_pricing` | 40 |

## `trusted_router.operational_analytics`

Frozen alias: `frozen_main.operational_analytics`. Member: `src/trusted_router/operational_analytics.py`.

SHA-256: `99e847e1f6746a0cf9ae6472211c42518983cc170aef97fc4b138ed640d4af42`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `OperationalAnalyticsClient` | 48 |

## `trusted_router.operational_analytics_freshness`

Frozen alias: `frozen_main.operational_analytics_freshness`. Member: `src/trusted_router/operational_analytics_freshness.py`.

SHA-256: `10d1c9d35610aae507b8fe13d4b6b073a7cc0c8b6179e31b29a3afa233790a4a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `OutboxFreshness` | 255 |
| `OutboxHeartbeat` | 192 |
| `__create_fn__` | 1 |

## `trusted_router.partner_billing`

Frozen alias: `frozen_main.partner_billing`. Member: `src/trusted_router/partner_billing.py`.

SHA-256: `3933997720aca1c8bab813ecdf4ec8ef8afb27c0f394e6fd738a4045e99a6123`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `PartnerBillingMode` | 20 |
| `partner_billing_mode` | 25 |

## `trusted_router.phone_verification`

Frozen alias: `frozen_main.phone_verification`. Member: `src/trusted_router/phone_verification.py`.

SHA-256: `6306c19436f7e6d384d74271c4579f860b1c26c469712f87286f265102e8bc91`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ConfirmResult` | 172 |
| `PhoneIdentityVerificationRequired` | 168 |
| `PhoneNumberError` | 164 |
| `__create_fn__` | 1 |

## `trusted_router.polyphemus`

Frozen alias: `frozen_main.polyphemus`. Member: `src/trusted_router/polyphemus.py`.

SHA-256: `bb10cfe35e31266365551f6e61a2802b442034107a34b20731bd740a220baf80`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.post_commit`

Frozen alias: `frozen_main.post_commit`. Member: `src/trusted_router/post_commit.py`.

SHA-256: `5dacfe38037e3aea07c68ad39a2b7d855b0f20b5a6eb250a3d4cbb6118166f3c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `PostCommitExecutor` | 21 |
| `PostCommitExecutor.__init__` | 29 |

## `trusted_router.pricing`

Frozen alias: `frozen_main.pricing`. Member: `src/trusted_router/pricing.py`.

SHA-256: `54c9997ef7841dd009769bfbba98d136ecbd489d4f7ebf67dc5b21383345f9d8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ModelPricingKwargs` | 119 |
| `PriceTier` | 27 |
| `PriceTier.__init__` | 2 |
| `RequestRates` | 50 |
| `__create_fn__` | 1 |
| `_as_positive_int` | 403 |
| `_customer_price` | 134 |
| `_customer_price_from_dollars_per_token` | 258 |
| `_flat_tier` | 58 |
| `_optional_customer_price_from_dollars_per_token` | 274 |
| `_priced` | 248 |
| `_provider_manifest_customer_price` | 144 |
| `_provider_manifest_exact_integer` | 456 |
| `_provider_manifest_optional_price_cost` | 433 |
| `_provider_manifest_price_cost` | 426 |
| `_provider_manifest_price_scale` | 413 |
| `_provider_manifest_price_tiers` | 583 |
| `_read_pricing_tiers` | 311 |
| `_strict_customer_price_from_dollars_per_token` | 294 |
| `cache_token_prices_microdollars` | 234 |
| `customer_fixed_price_microdollars` | 156 |
| `provider_manifest_price_profile_is_valid` | 530 |
| `provider_manifest_price_tiers_are_valid` | 469 |

## `trusted_router.provider_adapters`

Frozen alias: `frozen_main.provider_adapters`. Member: `src/trusted_router/provider_adapters.py`.

SHA-256: `ec43182c53bc13e6d13cea3963e6b86049b01476b59fa6e74d749ab371891b7f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.provider_analytics`

Frozen alias: `frozen_main.provider_analytics`. Member: `src/trusted_router/provider_analytics.py`.

SHA-256: `7bd312afd1c1a9354d6b95311225548a19820a4910d0f3c76003a9b718cbbec5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ClickHouseExport` | 34 |
| `ProviderAnalyticsClient` | 48 |
| `__create_fn__` | 1 |

## `trusted_router.provider_branding`

Frozen alias: `frozen_main.provider_branding`. Member: `src/trusted_router/provider_branding.py`.

SHA-256: `f0b624ee7ae31c9490cb9f6323dc8e81cc8142159d64bd53f15bbedbe7adaf5d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProviderBrand` | 15 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.provider_compat`

Frozen alias: `frozen_main.provider_compat`. Member: `src/trusted_router/provider_compat.py`.

SHA-256: `62a54a82224d05e9da68807eebd6beb04057b2be1752b16b7ad6a507246e05dc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.provider_contract`

Frozen alias: `frozen_main.provider_contract`. Member: `src/trusted_router/provider_contract.py`.

SHA-256: `f4c7210b2c28c0d8376577e07bfa0be0aa4144830aac367deb6711e48eb4f3a7`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.provider_contracts`

Frozen alias: `frozen_main.provider_contracts`. Member: `src/trusted_router/provider_contracts.py`.

SHA-256: `332c1d4f4aef1da728faaccc66fa1ac0d6198640491d403067d680624d0352ae`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 47 |
| `<module>` | 1 |
| `provider_model_operator_held` | 80 |
| `provider_model_uses_passthrough_retail_price` | 98 |

## `trusted_router.provider_lifecycle`

Frozen alias: `frozen_main.provider_lifecycle`. Member: `src/trusted_router/provider_lifecycle.py`.

SHA-256: `f7276f67fdd1a6d6bdf4b2f6e3a376dfb2636f6f0ea5d08338c31d06b2339a69`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProviderPrice` | 100 |
| `ProviderPrice.__init__` | 2 |
| `_Retirement` | 154 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |
| `_clock_override_permitted` | 1065 |
| `_deepseek_prices` | 1181 |
| `_deepseek_v41_effective_at` | 1173 |
| `_deepseek_v4_family` | 1140 |
| `_deepseek_v4_period` | 1150 |
| `_deepseek_v4_period.<locals>.<genexpr>` | 1165 |
| `_effective_time` | 1110 |
| `_tencent_period` | 116 |
| `_utc_now` | 1073 |
| `provider_catalog_revision` | 1097 |
| `provider_catalog_revision.<locals>.<genexpr>` | 1105 |
| `provider_catalog_revision.<locals>.<genexpr>` | 1107 |
| `provider_model_retired` | 1122 |
| `provider_price_microdollars` | 1279 |

## `trusted_router.provider_locations`

Frozen alias: `frozen_main.provider_locations`. Member: `src/trusted_router/provider_locations.py`.

SHA-256: `ab1734a9b86fd01350f078022494446cb61b04f5e8cd7b6cb649838122a164c8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Headquarters` | 233 |
| `InferenceLocations` | 19 |
| `InferenceLocations.__init__` | 2 |
| `ModelLocationSnapshot` | 269 |
| `ModelLocationSnapshot.__init__` | 2 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |
| `_telnyx_location_snapshot` | 318 |
| `inference_location_metadata` | 333 |
| `provider_inference_locations` | 265 |
| `provider_model_locations` | 327 |
| `telnyx_model_locations` | 283 |
| `telnyx_model_locations.<locals>.<genexpr>` | 312 |
| `telnyx_model_locations.<locals>.<genexpr>` | 314 |

## `trusted_router.provider_manifest_policy`

Frozen alias: `frozen_main.provider_manifest_policy`. Member: `src/trusted_router/provider_manifest_policy.py`.

SHA-256: `fcbe3a56b78e94f1ef4c01259aec03a373f188c1597493578d4c73025c418afa`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_provider_manifest_generated_deadline` | 117 |
| `_provider_manifest_row_price_is_valid` | 83 |
| `_provider_manifest_row_price_is_valid.<locals>.<genexpr>` | 95 |
| `decision_manifest_price_is_valid` | 64 |
| `provider_manifest_valid_until` | 140 |

## `trusted_router.provider_payloads`

Frozen alias: `frozen_main.provider_payloads`. Member: `src/trusted_router/provider_payloads.py`.

SHA-256: `c5a9b51292dfed2f1a2df2cbe65c5b8971b21c178cdbeb12493d804b8fc1c3a3`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.provider_precision`

Frozen alias: `frozen_main.provider_precision`. Member: `src/trusted_router/provider_precision.py`.

SHA-256: `40da9ae7acf668cbd8b23a13bff085c2ea0546847d69efbb2a87c1937218d569`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `PrecisionSource` | 24 |
| `PrecisionSource.__init__` | 2 |
| `PrecisionSource.__post_init__` | 30 |
| `ProviderPrecision` | 41 |
| `ProviderPrecision.__init__` | 2 |
| `ProviderPrecision.__post_init__` | 58 |
| `ProviderPrecision.__post_init__.<locals>.<genexpr>` | 65 |
| `ProviderPrecision.from_dict` | 76 |
| `ProviderPrecision.from_dict.<locals>.<genexpr>` | 80 |
| `ProviderPrecision.metadata` | 84 |
| `__create_fn__` | 1 |
| `_precision_index` | 94 |
| `endpoint_precision` | 112 |
| `endpoint_precision_metadata` | 116 |
| `endpoint_quantization` | 121 |

## `trusted_router.provider_reliability`

Frozen alias: `frozen_main.provider_reliability`. Member: `src/trusted_router/provider_reliability.py`.

SHA-256: `79258686897362b3698e55cc081c9b07baa15103314a797afc132e6a796bc7ef`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `FailureAttribution` | 46 |
| `FailureClass` | 31 |
| `FailureOwner` | 22 |
| `ModelDeadlines` | 178 |
| `__create_fn__` | 1 |

## `trusted_router.provider_streaming`

Frozen alias: `frozen_main.provider_streaming`. Member: `src/trusted_router/provider_streaming.py`.

SHA-256: `47c1fbd056a2269ab126280f4e81f9fb20555769977f3717de8d2a9e572ecc5c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.provider_types`

Frozen alias: `frozen_main.provider_types`. Member: `src/trusted_router/provider_types.py`.

SHA-256: `33e044dcfd39e49b8edbe33c257b1cb940434c00a27694be2689e8194ffa4bcd`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProviderError` | 81 |
| `ProviderResult` | 8 |
| `ProviderStreamState` | 30 |
| `__create_fn__` | 1 |

## `trusted_router.providers`

Frozen alias: `frozen_main.providers`. Member: `src/trusted_router/providers.py`.

SHA-256: `578094ad62c35ffdaa4e750ddb9e3fb7e03a617914ae494969e2f4aa2943aea9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProviderClient` | 137 |

## `trusted_router.public_analytics_snapshots`

Frozen alias: `frozen_main.public_analytics_snapshots`. Member: `src/trusted_router/public_analytics_snapshots.py`.

SHA-256: `c1d2a149855896e1fffb044bcbae1fd8ef6735e770334035558365ffe0dc9f08`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.public_catalog_aliases`

Frozen alias: `frozen_main.public_catalog_aliases`. Member: `src/trusted_router/public_catalog_aliases.py`.

SHA-256: `619346ca98764b2c4accda6ca7c18d0c78d8d9a87dcaca751bd6e1ad6ca93ffc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.public_openapi`

Frozen alias: `frozen_main.public_openapi`. Member: `src/trusted_router/public_openapi.py`.

SHA-256: `bb51b105464e2cd341a09f1b6692e94c98262a511c9d650edbf12fba9991fcb4`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `PublicOpenAPIPayload` | 75 |
| `__create_fn__` | 1 |
| `document_router_server` | 31 |
| `inference_servers` | 27 |
| `install_operation_servers` | 38 |

## `trusted_router.receipt_keys`

Frozen alias: `frozen_main.receipt_keys`. Member: `src/trusted_router/receipt_keys.py`.

SHA-256: `89d05662a37886284955f5723895282c58d7d911ccf05f3430cea105ca49f239`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.regions`

Frozen alias: `frozen_main.regions`. Member: `src/trusted_router/regions.py`.

SHA-256: `38d491f63e99b96e33e99a9e38195ce29c45137c513bb5dafd0195e2d279ab42`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `RegionGeo` | 9 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.request_attribution`

Frozen alias: `frozen_main.request_attribution`. Member: `src/trusted_router/request_attribution.py`.

SHA-256: `d881ed6fd83b7766fb314aab9ed72bbdef61853fd41c3d5ea23e419af3003acd`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InvalidAttribution` | 23 |
| `RequestAttribution` | 27 |
| `RequestAttribution.__init__` | 2 |
| `RequestAttribution.body_fields` | 36 |
| `__create_fn__` | 1 |
| `_bounded_string` | 80 |
| `_valid_categories` | 98 |
| `_valid_referer` | 88 |
| `_valid_trace` | 110 |
| `validate_request_attribution` | 53 |

## `trusted_router.request_body_limit`

Frozen alias: `frozen_main.request_body_limit`. Member: `src/trusted_router/request_body_limit.py`.

SHA-256: `f75518fc19ccbe958d9c025a98cf483131446dd22c890703926734dbdb7f13c5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `RequestBodyLimitMiddleware` | 79 |
| `RequestBodyLimitMiddleware.__call__` | 97 |
| `RequestBodyLimitMiddleware.__call__.<locals>.acquire_body_slot` | 120 |
| `RequestBodyLimitMiddleware.__call__.<locals>.limited_receive` | 154 |
| `RequestBodyLimitMiddleware.__call__.<locals>.limited_send` | 206 |
| `RequestBodyLimitMiddleware.__call__.<locals>.release_body_slot` | 114 |
| `RequestBodyLimitMiddleware.__call__.<locals>.reserve_to` | 127 |
| `RequestBodyLimitMiddleware.__init__` | 80 |
| `UnreadRequestBodyCloseMiddleware` | 253 |
| `UnreadRequestBodyCloseMiddleware.__call__` | 265 |
| `UnreadRequestBodyCloseMiddleware.__call__.<locals>.close_unread_send` | 279 |
| `UnreadRequestBodyCloseMiddleware.__call__.<locals>.observed_receive` | 272 |
| `UnreadRequestBodyCloseMiddleware.__init__` | 262 |
| `_BodyFraming` | 28 |
| `_BodyFraming.__init__` | 2 |
| `_BodyFraming.possible_body` | 34 |
| `_InFlightBodyBudget` | 47 |
| `_InFlightBodyBudget.__init__` | 50 |
| `_InFlightBodyBudget.release` | 64 |
| `_InFlightBodyBudget.reserve` | 55 |
| `__create_fn__` | 1 |
| `_body_framing` | 317 |
| `_scope_has_possible_body` | 381 |

## `trusted_router.request_capabilities`

Frozen alias: `frozen_main.request_capabilities`. Member: `src/trusted_router/request_capabilities.py`.

SHA-256: `c0eae8acfe972f634ce92dc7a73037765d401ac4b629266ff6aa0e43349ef239`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `RequestCapabilities` | 27 |
| `_reviewed_contracts` | 35 |
| `endpoint_capabilities` | 102 |
| `model_capabilities` | 118 |
| `model_capabilities.<locals>.<genexpr>` | 127 |
| `model_capabilities.<locals>.<genexpr>` | 138 |
| `normalize_request_capabilities` | 46 |
| `normalize_request_capabilities.<locals>.<genexpr>` | 70 |
| `normalize_request_capabilities.<locals>.<genexpr>` | 71 |
| `normalize_request_capabilities.<locals>.<genexpr>` | 78 |
| `normalize_request_capabilities.<locals>.<genexpr>` | 85 |
| `normalize_request_capabilities.<locals>.<genexpr>` | 92 |
| `normalize_request_capabilities.<locals>.<genexpr>` | 95 |

## `trusted_router.request_limits`

Frozen alias: `frozen_main.request_limits`. Member: `src/trusted_router/request_limits.py`.

SHA-256: `f15fa48daedeee4e7a3ddc9d3508674258e594e30b9aae65339e221d1d39b82d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `fingerprint_subject` | 76 |
| `normalized_client_identity` | 36 |

## `trusted_router.request_tags`

Frozen alias: `frozen_main.request_tags`. Member: `src/trusted_router/request_tags.py`.

SHA-256: `27756fea57f93fa98b9c9615c784342fd2a3a24ab02583e57a741523803dd8ed`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InvalidTags` | 18 |

## `trusted_router.routable_payouts`

Frozen alias: `frozen_main.routable_payouts`. Member: `src/trusted_router/routable_payouts.py`.

SHA-256: `03497a25a9f010a33dceb7d2a9612245ff09ffe350b32fa1e09645babc3b711c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.routes`

Frozen alias: `frozen_main.routes`. Member: `src/trusted_router/routes/__init__.py`.

SHA-256: `5c3c572faf2a17c70cdd7d15c793a6ce625ebd00867f8e1d7b5d94855e1c2f7b`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.routes.acquisition`

Frozen alias: `frozen_main.routes.acquisition`. Member: `src/trusted_router/routes/acquisition.py`.

SHA-256: `a46fea0b0c63eb76100946d767cfdd66c5a2fb9e9304f87d39fbcf6bb791001c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `MarketingEventRequest` | 11 |
| `register_acquisition_routes` | 71 |

## `trusted_router.routes.activity`

Frozen alias: `frozen_main.routes.activity`. Member: `src/trusted_router/routes/activity.py`.

SHA-256: `eda71d7dbd852c18583137f1a307fc701c29081f685d292f1caba81ebbfb4649`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_activity_routes` | 17 |

## `trusted_router.routes.auth`

Frozen alias: `frozen_main.routes.auth`. Member: `src/trusted_router/routes/auth.py`.

SHA-256: `19deb16812155cefa89e3617f438b678815f22dd88d145d3c1e3b9f6cef7ef6c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_auth_routes` | 24 |

## `trusted_router.routes.bedrock_group_buy`

Frozen alias: `frozen_main.routes.bedrock_group_buy`. Member: `src/trusted_router/routes/bedrock_group_buy.py`.

SHA-256: `4720495aea054d5d42b04a1de05e426b893dbb1c8e00d3b59b0cd056b0047793`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_invalidate_public_snapshot` | 291 |
| `register_bedrock_group_buy_control_routes` | 89 |
| `register_bedrock_group_buy_public_routes` | 41 |

## `trusted_router.routes.billing`

Frozen alias: `frozen_main.routes.billing`. Member: `src/trusted_router/routes/billing.py`.

SHA-256: `738760deb2a51f59ce1d290215879b6edd73cb6341ab998650cc0904530998f6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_billing_routes` | 53 |

## `trusted_router.routes.broadcast`

Frozen alias: `frozen_main.routes.broadcast`. Member: `src/trusted_router/routes/broadcast.py`.

SHA-256: `4110fe558a74fe2ec384a77ffd4dfd355890d361825ce7c4005706d401bbcae8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_broadcast_routes` | 30 |

## `trusted_router.routes.byok`

Frozen alias: `frozen_main.routes.byok`. Member: `src/trusted_router/routes/byok.py`.

SHA-256: `e438d60b9cc91e83c2a1a856b9f213692bd8752dd85d7be82942ff6b096dbf99`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_byok_routes` | 27 |

## `trusted_router.routes.catalog`

Frozen alias: `frozen_main.routes.catalog`. Member: `src/trusted_router/routes/catalog.py`.

SHA-256: `5ac09eb15ec9944e068392cacecb047ad5c6e04fa67dfeaa441a1df389192bab`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_PublicCatalogPayload` | 68 |
| `_PublicCatalogPayload.__init__` | 2 |
| `__create_fn__` | 1 |
| `_content_etag` | 90 |
| `_current_catalog_payload` | 158 |
| `_json_bytes` | 81 |
| `_picker_model_shape` | 99 |
| `_public_catalog_payload` | 128 |
| `register_authenticated_catalog_routes` | 663 |
| `register_catalog_routes` | 422 |

## `trusted_router.routes.chat_proxy`

Frozen alias: `frozen_main.routes.chat_proxy`. Member: `src/trusted_router/routes/chat_proxy.py`.

SHA-256: `1ed4cb1ff5cbcbbed3339923b85774208e7dd2fc998abc00cd324213b9ea5ee0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_chat_proxy_routes` | 92 |

## `trusted_router.routes.client_events`

Frozen alias: `frozen_main.routes.client_events`. Member: `src/trusted_router/routes/client_events.py`.

SHA-256: `87a0af0143b434d8e8835054206e1cacb03a22a80eb7e4ed01769dce49c3b073`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_client_events_routes` | 112 |

## `trusted_router.routes.compat`

Frozen alias: `frozen_main.routes.compat`. Member: `src/trusted_router/routes/compat.py`.

SHA-256: `b64b3a3e53fa31a7e15dccaaa3120ea86e3b2d618d022ac8511bc4ed4a617aeb`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_add_current_openrouter_stubs` | 93 |
| `_add_guardrail_stubs` | 74 |
| `register_compat_stub_routes` | 9 |
| `register_gateway_compat_stub_routes` | 36 |
| `register_versioned_compat_stub_routes` | 62 |

## `trusted_router.routes.console`

Frozen alias: `frozen_main.routes.console`. Member: `src/trusted_router/routes/console/__init__.py`.

SHA-256: `35dbe265ad00c2c98729859d8d9887efa2a40f13af21163cd0706af35172422a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_console_routes` | 43 |

## `trusted_router.routes.console._shared`

Frozen alias: `frozen_main.routes.console._shared`. Member: `src/trusted_router/routes/console/_shared.py`.

SHA-256: `2232a5fd3844f57f12b53104be531168f3706039264a04e0b295baacdf5565df`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ConsoleContext` | 34 |
| `__create_fn__` | 1 |

## `trusted_router.routes.console.activity`

Frozen alias: `frozen_main.routes.console.activity`. Member: `src/trusted_router/routes/console/activity.py`.

SHA-256: `a664b3dbb248f25083fc7c570d7317dc286250777bc6289328a407f6df0c0725`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_UsageCache` | 39 |
| `_UsageCache.__init__` | 42 |
| `register` | 201 |

## `trusted_router.routes.console.api_keys`

Frozen alias: `frozen_main.routes.console.api_keys`. Member: `src/trusted_router/routes/console/api_keys.py`.

SHA-256: `fb215f51e4bfa6fd30a62919124e89608cfa7d1376c0929a74c6ab92bc154f87`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 32 |

## `trusted_router.routes.console.authorized_apps`

Frozen alias: `frozen_main.routes.console.authorized_apps`. Member: `src/trusted_router/routes/console/authorized_apps.py`.

SHA-256: `86c15121a33e7f0802e4e2576345e3bce9ed94b3c4fcc78fb1a7512660277a59`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 20 |

## `trusted_router.routes.console.broadcast`

Frozen alias: `frozen_main.routes.console.broadcast`. Member: `src/trusted_router/routes/console/broadcast.py`.

SHA-256: `2ebdd6d9c9e9023f328b5202fcaed3d6c2783dc790e5d4161d3c672e51ffd9b2`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 27 |

## `trusted_router.routes.console.byok`

Frozen alias: `frozen_main.routes.console.byok`. Member: `src/trusted_router/routes/console/byok.py`.

SHA-256: `41ba512c5b4ec62ba5369dcc9b82d3ed56b903cf7f1b3fec1c9f987b538b071b`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 20 |

## `trusted_router.routes.console.credit_transfers`

Frozen alias: `frozen_main.routes.console.credit_transfers`. Member: `src/trusted_router/routes/console/credit_transfers.py`.

SHA-256: `eb86f8d20bd1047a9aff08463d9e4cc166cfb2638b527454c646bda935de95b1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 18 |

## `trusted_router.routes.console.credits`

Frozen alias: `frozen_main.routes.console.credits`. Member: `src/trusted_router/routes/console/credits.py`.

SHA-256: `d99ad03bcda5c50588d1eaa16438722c7b1f585d47927e20cb74f92be6b2cc26`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 61 |

## `trusted_router.routes.console.custom_models`

Frozen alias: `frozen_main.routes.console.custom_models`. Member: `src/trusted_router/routes/console/custom_models.py`.

SHA-256: `12a9dae37792c0a2049df569755ac087116122ecd0ba3e0dc152e9385a4485ba`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 25 |

## `trusted_router.routes.console.earnings`

Frozen alias: `frozen_main.routes.console.earnings`. Member: `src/trusted_router/routes/console/earnings.py`.

SHA-256: `4ccb75198a12a845c4e2dc4a3449265cf3a67c3834587eaea3c4224cdbd80e3f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 23 |

## `trusted_router.routes.console.preferences`

Frozen alias: `frozen_main.routes.console.preferences`. Member: `src/trusted_router/routes/console/preferences.py`.

SHA-256: `d3a5305dd61e2527490ca544d9308828c9c908cf5d199e109b2c45d0e2bb0ccd`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 12 |

## `trusted_router.routes.console.root`

Frozen alias: `frozen_main.routes.console.root`. Member: `src/trusted_router/routes/console/root.py`.

SHA-256: `2f6bd0557b43684bb734d1fcd7d617904d0c4188bd0f9b718560f8781a0bd694`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 18 |

## `trusted_router.routes.console.routing_page`

Frozen alias: `frozen_main.routes.console.routing_page`. Member: `src/trusted_router/routes/console/routing_page.py`.

SHA-256: `7d14280700a0a0576eef84979ff6c4a08f8c9959edc8f9f071ec0ec6ee1e8370`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 13 |

## `trusted_router.routes.console.settings`

Frozen alias: `frozen_main.routes.console.settings`. Member: `src/trusted_router/routes/console/settings.py`.

SHA-256: `4e5fce52a3e85445a2a029703b046f3b70f0bb5c1e510795ebf10103104fb0e8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 33 |

## `trusted_router.routes.console.user_models`

Frozen alias: `frozen_main.routes.console.user_models`. Member: `src/trusted_router/routes/console/user_models.py`.

SHA-256: `89760fb0f194dfcf487fdd6c0cde350c488fbca4920e32edc007f1f89873aadd`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 36 |

## `trusted_router.routes.console.verification`

Frozen alias: `frozen_main.routes.console.verification`. Member: `src/trusted_router/routes/console/verification.py`.

SHA-256: `b6512174db83255314204c8a08d0ac6cff5e830ecbe6e37df41f326ab3aee74b`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 24 |

## `trusted_router.routes.console.welcome`

Frozen alias: `frozen_main.routes.console.welcome`. Member: `src/trusted_router/routes/console/welcome.py`.

SHA-256: `d8e62ada6d7c48b28494cf31d7dec36dae994722ab9828a202dfcf559e7639b6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 15 |

## `trusted_router.routes.credit_transfers`

Frozen alias: `frozen_main.routes.credit_transfers`. Member: `src/trusted_router/routes/credit_transfers.py`.

SHA-256: `79d46ebf306060aceb2a60ed88541f584d679fc627e295d80959192781910934`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_credit_transfer_routes` | 50 |

## `trusted_router.routes.custom_models`

Frozen alias: `frozen_main.routes.custom_models`. Member: `src/trusted_router/routes/custom_models.py`.

SHA-256: `c31ddb26a2d51e9c71301159b701e7615f21aa43ca69c92ca917a073df866d15`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_custom_model_routes` | 29 |

## `trusted_router.routes.email_verify`

Frozen alias: `frozen_main.routes.email_verify`. Member: `src/trusted_router/routes/email_verify.py`.

SHA-256: `87268dedca5dee8256affe67d8b8a4b6e28bd3a1284c605a5769c8a33b138b18`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_email_verify_routes` | 24 |

## `trusted_router.routes.helpers`

Frozen alias: `frozen_main.routes.helpers`. Member: `src/trusted_router/routes/helpers.py`.

SHA-256: `809759e3857fd6a4bb92257f1e30d513cc8f79376fb570d0180f9c380c6e239e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.routes.identity_verify`

Frozen alias: `frozen_main.routes.identity_verify`. Member: `src/trusted_router/routes/identity_verify.py`.

SHA-256: `9ab92cea60d44f57cefc249744a1db2334db07d8053e533b56e54f3e51e22257`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `IdentitySessionResult` | 44 |
| `__create_fn__` | 1 |
| `register_identity_verify_routes` | 26 |

## `trusted_router.routes.inference`

Frozen alias: `frozen_main.routes.inference`. Member: `src/trusted_router/routes/inference.py`.

SHA-256: `2f8b327f9363d711373001f116a3b712cbd3c8c7ff565af03f06319dc8fbc7b0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_inference_routes` | 152 |

## `trusted_router.routes.internal`

Frozen alias: `frozen_main.routes.internal`. Member: `src/trusted_router/routes/internal/__init__.py`.

SHA-256: `cfbf6687c5cd8e7320f0d0744524e0eec8fd1ac27b754b7695173411fc3cc6c5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_control_internal_routes` | 38 |
| `register_external_webhook_routes` | 29 |
| `register_gateway_internal_routes` | 43 |
| `register_internal_routes` | 65 |
| `register_observer_internal_routes` | 59 |

## `trusted_router.routes.internal._shared`

Frozen alias: `frozen_main.routes.internal._shared`. Member: `src/trusted_router/routes/internal/_shared.py`.

SHA-256: `68294d0fb5bdf42a524d9be8dcb4c783bec9473ed6e53117b68acc05e88c1b08`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `internal_service_credential` | 26 |
| `require_internal_gateway` | 53 |

## `trusted_router.routes.internal.admin`

Frozen alias: `frozen_main.routes.internal.admin`. Member: `src/trusted_router/routes/internal/admin.py`.

SHA-256: `0a99619b4b71525464079fd0e9093091cf6553676df27ff94e6f1288bfe9727a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `is_operator_route` | 122 |

## `trusted_router.routes.internal.adyen`

Frozen alias: `frozen_main.routes.internal.adyen`. Member: `src/trusted_router/routes/internal/adyen.py`.

SHA-256: `325205edb904de0abeaa2355923729f55672f3f90147598dd72fcb1337a352db`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 22 |

## `trusted_router.routes.internal.broadcast_queue`

Frozen alias: `frozen_main.routes.internal.broadcast_queue`. Member: `src/trusted_router/routes/internal/broadcast_queue.py`.

SHA-256: `2e37c998ce090c5c09c8b7a8631012dd98d72bd0d9dcca5eeae69dcfce0d766a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 13 |

## `trusted_router.routes.internal.chat_browser_key`

Frozen alias: `frozen_main.routes.internal.chat_browser_key`. Member: `src/trusted_router/routes/internal/chat_browser_key.py`.

SHA-256: `a0e31f2e981a7e2376ee0b86e657859ae74e9dbb14b2e1b8bc854673d780e52c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 72 |

## `trusted_router.routes.internal.federation`

Frozen alias: `frozen_main.routes.internal.federation`. Member: `src/trusted_router/routes/internal/federation.py`.

SHA-256: `858dd50225e8605090bacc4ca00342aa56ba02151586f46db7901aab4dc5b3ab`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 119 |

## `trusted_router.routes.internal.fetch_image`

Frozen alias: `frozen_main.routes.internal.fetch_image`. Member: `src/trusted_router/routes/internal/fetch_image.py`.

SHA-256: `e6b0594ab736e148eef27ba9ae02c5e3b51d80e3ae768176fec9ac2ebad5d7a9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 92 |

## `trusted_router.routes.internal.gateway`

Frozen alias: `frozen_main.routes.internal.gateway`. Member: `src/trusted_router/routes/internal/gateway.py`.

SHA-256: `baf12525b8e5c89c7c388bb9ee1b1b043c0afcaf8cfe754fd801089541bbb5a5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_actual_service_tier_or_error` | 5145 |
| `_authorization_is_synthetic` | 4864 |
| `_authorized_user_model_pair` | 4962 |
| `_endpoint_cost_microdollars` | 5345 |
| `_endpoint_cost_microdollars_from_document` | 5386 |
| `_endpoint_for_id_compat` | 5099 |
| `_intent_durable_gateway_data` | 4750 |
| `_is_native_batch_idempotency_key` | 5270 |
| `_is_native_batch_route` | 5191 |
| `_is_synthetic_settlement` | 4869 |
| `_native_batch_cost_or_error` | 5290 |
| `_partner_billing_mode_or_error` | 5408 |
| `_provider_price_tier_input_tokens` | 5334 |
| `_record_user_model_gateway_outcome_safely` | 4641 |
| `_refund_benchmark_sample_safely` | 3385 |
| `_release_user_model_slot_safely` | 4626 |
| `_schedule_auto_refill` | 5424 |
| `_select_authorized_endpoint` | 5055 |
| `_settle_body_with_safe_attribution` | 4772 |
| `_settle_body_with_safe_client_context` | 4809 |
| `_settle_gateway_authorization` | 3481 |
| `_settle_gateway_authorization.<locals>.<genexpr>` | 4359 |
| `_settle_gateway_with_admission_sync` | 3433 |
| `_settle_repair_metadata` | 4840 |
| `register` | 2234 |
| `register.<locals>.gateway_refund` | 2342 |
| `register.<locals>.gateway_settle` | 2333 |
| `settle_gateway` | 527 |

## `trusted_router.routes.internal.lightning`

Frozen alias: `frozen_main.routes.internal.lightning`. Member: `src/trusted_router/routes/internal/lightning.py`.

SHA-256: `ff266da36f6af7309ca3e946af617e524df3d47501b9fec1ed010894be81013a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Account` | 30 |
| `Body` | 17 |
| `Credit` | 34 |
| `Lookup` | 26 |
| `Resolve` | 21 |
| `register` | 48 |

## `trusted_router.routes.internal.paypal`

Frozen alias: `frozen_main.routes.internal.paypal`. Member: `src/trusted_router/routes/internal/paypal.py`.

SHA-256: `6d98d16f55361ec27001ba1dadfb0a4baa37646f40d30f2c1f87a08bc5a2bcb9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 22 |

## `trusted_router.routes.internal.reconcile`

Frozen alias: `frozen_main.routes.internal.reconcile`. Member: `src/trusted_router/routes/internal/reconcile.py`.

SHA-256: `385e32b057565132496c8f045e708142865edb6f9b522a3a5ce21fa33404a4ec`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 13 |

## `trusted_router.routes.internal.routable`

Frozen alias: `frozen_main.routes.internal.routable`. Member: `src/trusted_router/routes/internal/routable.py`.

SHA-256: `1fce2134e757c37dde60582aa846047d935d96ff8c8844b10a7f46af9a2c0071`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 23 |

## `trusted_router.routes.internal.sentry`

Frozen alias: `frozen_main.routes.internal.sentry`. Member: `src/trusted_router/routes/internal/sentry.py`.

SHA-256: `36b48f4978b80d41a2a7abb7de88d9f2eb96565a3b859f0dc56d007088c0a749`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 15 |

## `trusted_router.routes.internal.speculation`

Frozen alias: `frozen_main.routes.internal.speculation`. Member: `src/trusted_router/routes/internal/speculation.py`.

SHA-256: `c0917337d8f336e54a76f9e2d29c517f8cc595736018715f400da0021aaf36fa`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 17 |

## `trusted_router.routes.internal.synthetic`

Frozen alias: `frozen_main.routes.internal.synthetic`. Member: `src/trusted_router/routes/internal/synthetic.py`.

SHA-256: `503c62e8484adf2cc197cbfe1894b9160712f0ff5ae4d5c4c668fd5136c9ee9d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_HeldOperationSlot` | 77 |
| `register` | 488 |

## `trusted_router.routes.internal.veriff`

Frozen alias: `frozen_main.routes.internal.veriff`. Member: `src/trusted_router/routes/internal/veriff.py`.

SHA-256: `838407394aa8a3404d2e361e72ccb9572421ce4d7e099437a84e755924a154ee`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 21 |

## `trusted_router.routes.internal.video_jobs`

Frozen alias: `frozen_main.routes.internal.video_jobs`. Member: `src/trusted_router/routes/internal/video_jobs.py`.

SHA-256: `07eb5600af6e7a8f2b10969950c6a37b7bf1ab03f7b03980619c4f158abf980f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 212 |

## `trusted_router.routes.internal.webhook`

Frozen alias: `frozen_main.routes.internal.webhook`. Member: `src/trusted_router/routes/internal/webhook.py`.

SHA-256: `4f8307e9aee19a5305c8f09ca40b835e3bedf08da821a4f51e4ce609f475fec6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register` | 43 |

## `trusted_router.routes.keys`

Frozen alias: `frozen_main.routes.keys`. Member: `src/trusted_router/routes/keys.py`.

SHA-256: `f3dfe934806670bac26a87b857a222c39255aac43c9a7ebaa2a1d9d097dfe892`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_key_routes` | 88 |

## `trusted_router.routes.lightning_support`

Frozen alias: `frozen_main.routes.lightning_support`. Member: `src/trusted_router/routes/lightning_support.py`.

SHA-256: `add8d903c42a99b6903d068dedc1adddc70d562bf71289a56b7497efc13e3421`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Feedback` | 18 |
| `register_lightning_support_routes` | 24 |

## `trusted_router.routes.mcp`

Frozen alias: `frozen_main.routes.mcp`. Member: `src/trusted_router/routes/mcp.py`.

SHA-256: `9ff3e5a2858562b4a63537257ef6557387a6a204736d37615b2c4005bb807555`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `MCPToolError` | 543 |
| `TrustedRouterMCP` | 90 |
| `TrustedRouterMCP.__init__` | 91 |
| `_MCPAuth` | 65 |
| `__create_fn__` | 1 |
| `register_mcp_routes` | 71 |

## `trusted_router.routes.mcp_advisor`

Frozen alias: `frozen_main.routes.mcp_advisor`. Member: `src/trusted_router/routes/mcp_advisor.py`.

SHA-256: `9c9a0bb46515fb15ad27f5a28178ca526a9544a206d0a63a4bf66f49e4a780c9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Arguments` | 51 |
| `Compare` | 64 |
| `Docs` | 81 |
| `Estimate` | 69 |
| `ProviderLookup` | 77 |
| `Search` | 55 |
| `register_advisor_mcp_routes` | 340 |

## `trusted_router.routes.model_changes`

Frozen alias: `frozen_main.routes.model_changes`. Member: `src/trusted_router/routes/model_changes.py`.

SHA-256: `ed2cfa2d14be4e83dbe38fe0a3c1b10d4aa59b01e9def554fa08e3f09d441bdb`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AtomResponse` | 73 |
| `CatalogChange` | 27 |
| `CatalogChanges` | 40 |
| `register_model_change_routes` | 77 |

## `trusted_router.routes.notify`

Frozen alias: `frozen_main.routes.notify`. Member: `src/trusted_router/routes/notify.py`.

SHA-256: `b07c0b68af8b07bc3d781053ee3b02b3d20aee99d407fe77a7f275b075117123`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_Charge` | 287 |
| `register_notify_public_routes` | 58 |
| `register_notify_routes` | 83 |

## `trusted_router.routes.oauth`

Frozen alias: `frozen_main.routes.oauth`. Member: `src/trusted_router/routes/oauth.py`.

SHA-256: `e0eb3d6a7935a2847f2d5cd58fb40d375f653d1108eef0749706f40d2c29da1c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_PersistedLogin` | 147 |
| `__create_fn__` | 1 |
| `_register_provider` | 76 |
| `register_oauth_routes` | 71 |

## `trusted_router.routes.oauth_apps`

Frozen alias: `frozen_main.routes.oauth_apps`. Member: `src/trusted_router/routes/oauth_apps.py`.

SHA-256: `70207fdd6c9ea45d715d09512e15f67f11d7af010836eb5656df0b27449c0e35`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_oauth_app_routes` | 79 |

## `trusted_router.routes.oauth_authorized_apps`

Frozen alias: `frozen_main.routes.oauth_authorized_apps`. Member: `src/trusted_router/routes/oauth_authorized_apps.py`.

SHA-256: `8ec81a900e4c6ed0a65fc2902320da5f7d4abe6e815f2fa6ebea560963d45de1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_oauth_authorized_app_routes` | 45 |

## `trusted_router.routes.oauth_keys`

Frozen alias: `frozen_main.routes.oauth_keys`. Member: `src/trusted_router/routes/oauth_keys.py`.

SHA-256: `033ffb106bdf540541157192c783e1fb6d9a1c4436e6849faec2f1938ddbc586`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_oauth_key_routes` | 68 |

## `trusted_router.routes.payouts`

Frozen alias: `frozen_main.routes.payouts`. Member: `src/trusted_router/routes/payouts.py`.

SHA-256: `9e0a4ad78eaee57efdacdf0749e08f6be6932e7fe5291021864686c13ff1f88a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_OnboardingValues` | 47 |
| `register_payout_routes` | 55 |

## `trusted_router.routes.provider_portal`

Frozen alias: `frozen_main.routes.provider_portal`. Member: `src/trusted_router/routes/provider_portal.py`.

SHA-256: `c6cae976629ff2b977a471d09bc48adbd6e3f8f5f97f72cfa5b3955b861e468b`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProviderPortalContext` | 31 |
| `__create_fn__` | 1 |
| `register_provider_portal_routes` | 90 |

## `trusted_router.routes.public`

Frozen alias: `frozen_main.routes.public`. Member: `src/trusted_router/routes/public.py`.

SHA-256: `f3200bfd197a336afe5dbf2e01eb82e63adc43fedde89e8d5e682066f0c0bdb8`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 785 |
| `<module>` | 1 |
| `_CachedPublicBody` | 278 |
| `_CachedStaticFiles` | 286 |
| `_CachedStaticFiles.__init__` | 302 |
| `_UnfilteredReceiptKeyCursor` | 438 |
| `__create_fn__` | 1 |
| `register_public_action_routes` | 926 |
| `register_public_routes` | 946 |
| `register_public_routes.<locals>.public_html_route` | 1056 |
| `register_public_routes.<locals>.public_html_route.<locals>.decorator` | 1059 |

## `trusted_router.routes.ses_notifications`

Frozen alias: `frozen_main.routes.ses_notifications`. Member: `src/trusted_router/routes/ses_notifications.py`.

SHA-256: `460318dcbe5db5e31efe8819905c74f19f32c2a4c60d1131aafaf69c86d9613b`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_ses_notification_routes` | 49 |

## `trusted_router.routes.settlements`

Frozen alias: `frozen_main.routes.settlements`. Member: `src/trusted_router/routes/settlements.py`.

SHA-256: `a6a6247961d8b6c6e03d9c325fcdfa938a5c4ade09eae1ef621cc261c7b7c948`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AsyncSettlementRoute` | 27 |
| `AsyncSettlementRoute.get_route_handler` | 28 |
| `AsyncSettlementRoute.get_route_handler.<locals>.dispatch` | 31 |
| `register_settlement_routes` | 73 |

## `trusted_router.routes.signup`

Frozen alias: `frozen_main.routes.signup`. Member: `src/trusted_router/routes/signup.py`.

SHA-256: `6f3138ea943d85480669a0deb177aef691697f0008695e99b7254bdd46cc3d8d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_signup_routes` | 15 |

## `trusted_router.routes.user_models`

Frozen alias: `frozen_main.routes.user_models`. Member: `src/trusted_router/routes/user_models.py`.

SHA-256: `f73a0f5a1f237df585b45f3defe388b4f7453c6677077589fca7398e3bf27b11`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_user_model_routes` | 53 |

## `trusted_router.routes.user_models_public`

Frozen alias: `frozen_main.routes.user_models_public`. Member: `src/trusted_router/routes/user_models_public.py`.

SHA-256: `fb8ca6c11ebebb52acc37c59b279649ed6fb6269836dac9a2b94f34b387fefef`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_user_model_public_routes` | 14 |

## `trusted_router.routes.verification_status`

Frozen alias: `frozen_main.routes.verification_status`. Member: `src/trusted_router/routes/verification_status.py`.

SHA-256: `af4212b91b60e59f609ff566c7d97663f41c9c116e33ade7079c69f2cb131b29`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_verification_status_routes` | 23 |

## `trusted_router.routes.wallet_oauth`

Frozen alias: `frozen_main.routes.wallet_oauth`. Member: `src/trusted_router/routes/wallet_oauth.py`.

SHA-256: `102b80e960501e3cfb355c6dbdc8e34c7907b4dfbdedcc0d17d921c86a208167`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `WalletChallengeRequest` | 46 |
| `WalletVerifyRequest` | 51 |
| `register_wallet_oauth_routes` | 58 |

## `trusted_router.routes.workspaces`

Frozen alias: `frozen_main.routes.workspaces`. Member: `src/trusted_router/routes/workspaces.py`.

SHA-256: `ef4811378f0f8c1c841d3a00b49d5da0d9fad89d0bfedf4f341011c28d541585`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `register_workspace_routes` | 20 |

## `trusted_router.routing`

Frozen alias: `frozen_main.routing`. Member: `src/trusted_router/routing.py`.

SHA-256: `aecc2d0cf6b593878d0815b1b216663252cccd76eb1410fbeaff74f7bf1f3649`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `NormalizedRoutingInputs` | 76 |
| `RoutePreferences` | 49 |
| `RoutingCandidates` | 248 |
| `__create_fn__` | 1 |

## `trusted_router.routing_candidates`

Frozen alias: `frozen_main.routing_candidates`. Member: `src/trusted_router/routing_candidates.py`.

SHA-256: `6cf0de2a48740c566076aed0e98aa848f1c143f3a37741a804fc0ca5402626ed`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InvalidAutoModelOrder` | 109 |
| `_is_regular_chat_model` | 542 |
| `_meta_route_kind` | 478 |
| `_models_for_ids` | 372 |
| `_price_sort_key` | 550 |
| `_privacy_candidate_models` | 261 |
| `_privacy_candidate_models.<locals>.<lambda>` | 307 |
| `_privacy_candidate_models.<locals>.<lambda>` | 319 |
| `auto_candidate_models` | 137 |
| `cheap_candidate_models` | 166 |
| `e2e_candidate_models` | 364 |
| `eu_candidate_models` | 330 |
| `fast_candidate_models` | 199 |
| `free_candidate_models` | 156 |
| `green_candidate_models` | 339 |
| `meta_candidate_models` | 396 |
| `monitor_candidate_models` | 215 |
| `prometheus_1m_candidate_models` | 383 |
| `validate_auto_model_order` | 113 |
| `validate_auto_model_order.<locals>.<genexpr>` | 129 |
| `zdr_candidate_models` | 348 |

## `trusted_router.routing_state`

Frozen alias: `frozen_main.routing_state`. Member: `src/trusted_router/routing_state.py`.

SHA-256: `faf6be9fc7159c0a88bcbec1ef592f265e9cf009be80c72d54f5341de24158a3`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `RoutingState` | 71 |
| `RoutingState.__init__` | 72 |

## `trusted_router.schemas`

Frozen alias: `frozen_main.schemas`. Member: `src/trusted_router/schemas.py`.

SHA-256: `f1e3ad90cfb75346ef1bc9357cc2faa569ad815d06fed77149ad584607257332`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BroadcastDestinationCreateRequest` | 193 |
| `BroadcastDestinationPatchRequest` | 209 |
| `BulkDeleteKeysRequest` | 163 |
| `CheckoutRequest` | 66 |
| `CreateKeyRequest` | 139 |
| `CreatorUsernameRequest` | 62 |
| `CreditTransferRequest` | 277 |
| `CustomModelCreateRequest` | 219 |
| `CustomModelPatchRequest` | 228 |
| `EarningsTransferRequest` | 271 |
| `GatewayAuthorizeData` | 330 |
| `GatewayAuthorizeRequest` | 365 |
| `GatewayAuthorizeResponse` | 350 |
| `GatewayBootCapabilities` | 299 |
| `GatewayBootRegistrationRequest` | 311 |
| `GatewayClientContext` | 536 |
| `GatewayContractRejection` | 484 |
| `GatewayFetchImageRequest` | 427 |
| `GatewayHeartbeatRequest` | 672 |
| `GatewayHeartbeatUsage` | 639 |
| `GatewayResolveCustomModelRequest` | 517 |
| `GatewaySettleData` | 354 |
| `GatewaySettleRequest` | 560 |
| `GatewaySettleRequest.cache_creation_count` | 626 |
| `GatewaySettleRequest.cache_read_count` | 622 |
| `GatewaySettleRequest.input_count` | 610 |
| `GatewaySettleRequest.output_count` | 616 |
| `GatewaySettleRequest.selected_endpoint_id` | 634 |
| `GatewaySettleResponse` | 361 |
| `GatewayTimingData` | 319 |
| `GatewayValidateRequest` | 504 |
| `GatewayVideoJobClaimRequest` | 466 |
| `GatewayVideoJobLookupRequest` | 462 |
| `GatewayVideoJobPrepareRequest` | 434 |
| `GatewayVideoJobQueuedRequest` | 451 |
| `GatewayVideoJobUpdateRequest` | 472 |
| `PatchKeyRequest` | 167 |
| `ReconcileGenerationActivityRequest` | 530 |
| `SignupRequest` | 57 |
| `UpsertByokRequest` | 186 |
| `UserModelCreateRequest` | 237 |
| `UserModelPatchRequest` | 254 |
| `X402FundingRequest` | 107 |
| `X402SettleRequest` | 123 |
| `_Lenient` | 51 |
| `_Strict` | 45 |

## `trusted_router.scopes`

Frozen alias: `frozen_main.scopes`. Member: `src/trusted_router/scopes.py`.

SHA-256: `69208a5623de4c3f59b53b2f8c72121c80e68540af017870b208b77d08444b85`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.secrets`

Frozen alias: `frozen_main.secrets`. Member: `src/trusted_router/secrets.py`.

SHA-256: `ca85821fc9589906cafaa9ae62c87ea22dc38708472b5c606e9e927c3e0d6ea6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `LocalKeyFile` | 19 |

## `trusted_router.security`

Frozen alias: `frozen_main.security`. Member: `src/trusted_router/security.py`.

SHA-256: `c0b26132d312213a4e773dff588b3437d537f26fe2062f5cb16d51e1cd537a1a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.sentry_config`

Frozen alias: `frozen_main.sentry_config`. Member: `src/trusted_router/sentry_config.py`.

SHA-256: `23e81d7aa5cef89ff5b9967eb792013f74b083ee80308f3871ae58f3d23c27ae`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SentryFloodgateConfig` | 103 |
| `_FloodBucket` | 113 |
| `_SentryFloodgate` | 119 |
| `_SentryFloodgate.__init__` | 120 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |
| `init_sentry` | 226 |
| `sentry_should_init` | 276 |

## `trusted_router.seo_catalog`

Frozen alias: `frozen_main.seo_catalog`. Member: `src/trusted_router/seo_catalog.py`.

SHA-256: `12aa5b110ba5f01d17007a19a18ac60256dee9d62a3c8db53425145ab49a83b5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.seo_meta`

Frozen alias: `frozen_main.seo_meta`. Member: `src/trusted_router/seo_meta.py`.

SHA-256: `bbc85892b781625fca0a29eae7fa39516e70d9b888728a31dde22b3f8d131237`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.serialization`

Frozen alias: `frozen_main.serialization`. Member: `src/trusted_router/serialization.py`.

SHA-256: `b55908e9185a1852e285e4c2a290001156f2d0a92e2031998aac69d03c943db0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services`

Frozen alias: `frozen_main.services`. Member: `src/trusted_router/services/__init__.py`.

SHA-256: `755a30a153222d94db5bc6114d96ca3ec39962a064a03e0f0b000b287addec5e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.adyen_billing`

Frozen alias: `frozen_main.services.adyen_billing`. Member: `src/trusted_router/services/adyen_billing.py`.

SHA-256: `41d8ededf5c96d4b7547bd4b726f44177a040d87f02f3b0881e66c2cce24c288`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AdyenCheckoutReference` | 72 |
| `AdyenCreditResult` | 61 |
| `PreparedAdyenNotification` | 79 |
| `__create_fn__` | 1 |

## `trusted_router.services.async_settle`

Frozen alias: `frozen_main.services.async_settle`. Member: `src/trusted_router/services/async_settle.py`.

SHA-256: `8194bd496e5d7f9ad96c0d8cbc2c1170ce0ce5615fb44b5f5eeedb357dc87559`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Admission` | 39 |
| `AdmissionCache` | 52 |
| `AdmissionCache.__init__` | 53 |
| `DrainHealth` | 45 |
| `DrainHealth.__init__` | 2 |
| `Runtime` | 178 |
| `Runtime.__init__` | 2 |
| `__create_fn__` | 1 |
| `load_runtime` | 186 |

## `trusted_router.services.async_settle_handler`

Frozen alias: `frozen_main.services.async_settle_handler`. Member: `src/trusted_router/services/async_settle_handler.py`.

SHA-256: `a72e6a24bf63c2f899dbd7cce51ae1dde7fbb9317ce9374cc9151d357976c4bc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Input` | 74 |
| `Input.__init__` | 2 |
| `__create_fn__` | 1 |
| `_handle` | 226 |
| `handle` | 348 |
| `intent` | 136 |
| `intent.<locals>.<genexpr>` | 138 |
| `metric` | 30 |
| `parse` | 83 |
| `price` | 118 |
| `sync_required` | 37 |
| `verify` | 98 |
| `verify.<locals>.<genexpr>` | 105 |

## `trusted_router.services.auto_refill`

Frozen alias: `frozen_main.services.auto_refill`. Member: `src/trusted_router/services/auto_refill.py`.

SHA-256: `be89c4dff7d8cab05fd0effb3c239173332b04d2ad382101e1329936b0f80656`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AutoRefillOutcome` | 45 |
| `AutoRefillOutcome.__init__` | 2 |
| `__create_fn__` | 1 |
| `maybe_charge_after_settle` | 58 |
| `settlement_auto_refill_idempotency_key` | 53 |

## `trusted_router.services.broadcast`

Frozen alias: `frozen_main.services.broadcast`. Member: `src/trusted_router/services/broadcast.py`.

SHA-256: `84c0bbffd77271eb372f29406a06e41116c49fdf1e7076d4b725ae56da395efb`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `drain_broadcast_queue` | 78 |
| `enqueue_metadata_broadcast` | 62 |
| `should_drain_inline` | 120 |

## `trusted_router.services.broadcast_adapters`

Frozen alias: `frozen_main.services.broadcast_adapters`. Member: `src/trusted_router/services/broadcast_adapters.py`.

SHA-256: `a98ef0a2bc50483127812ff8661149a198c5da2e664b3259b74701c58a5d287d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BroadcastAdapter` | 18 |
| `PostHogBroadcastAdapter` | 42 |
| `WebhookOTLPBroadcastAdapter` | 101 |

## `trusted_router.services.budget_alerts`

Frozen alias: `frozen_main.services.budget_alerts`. Member: `src/trusted_router/services/budget_alerts.py`.

SHA-256: `b49827aaadcfe88f62cc6f8ad193e666a889a20e8307efbc5fa43800e9524838`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `maybe_send_budget_alerts` | 32 |

## `trusted_router.services.credit_transfer`

Frozen alias: `frozen_main.services.credit_transfer`. Member: `src/trusted_router/services/credit_transfer.py`.

SHA-256: `cd2e4c186e0eb50099bfe0f6abcdcaab1ccfcf536424c5f02c00f0373f873e4c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `CreditTransferClient` | 72 |
| `CreditTransferUnavailable` | 63 |

## `trusted_router.services.email`

Frozen alias: `frozen_main.services.email`. Member: `src/trusted_router/services/email.py`.

SHA-256: `022dca9a90c860b4e8274844ff3d71ee60a3f13a29616cc67dc331021e5737b3`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `EmailMessage` | 30 |
| `EmailService` | 44 |
| `__create_fn__` | 1 |

## `trusted_router.services.federation`

Frozen alias: `frozen_main.services.federation`. Member: `src/trusted_router/services/federation.py`.

SHA-256: `393ff92cf63c6b73fc48a8eede5fbc9298f741a811a62020fce1217a4975c751`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `FederationClient` | 116 |
| `FederationUnavailable` | 77 |
| `_Breaker` | 93 |
| `_InFlight` | 84 |
| `__create_fn__` | 1 |

## `trusted_router.services.inference`

Frozen alias: `frozen_main.services.inference`. Member: `src/trusted_router/services/inference.py`.

SHA-256: `8ea09c6d8280a3190b23fa5e4b62f8e146544a4a5ad30bf2b02eb77e065bb8ee`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.inference_errors`

Frozen alias: `frozen_main.services.inference_errors`. Member: `src/trusted_router/services/inference_errors.py`.

SHA-256: `a00ee3cc15c089d2f5c87922f93a9961e39c201041b4d61b096136cf878c5cb6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.inference_quota`

Frozen alias: `frozen_main.services.inference_quota`. Member: `src/trusted_router/services/inference_quota.py`.

SHA-256: `dd1c99744704da78ef03a5dbc4fc52ce70fe6ae4059f67ca7c633003b852b828`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `QuotaTicket` | 41 |
| `__create_fn__` | 1 |

## `trusted_router.services.keyed_admission`

Frozen alias: `frozen_main.services.keyed_admission`. Member: `src/trusted_router/services/keyed_admission.py`.

SHA-256: `58b7910f6e92fd3d1eb37bbf8321d4622767c69bec5a53eede7ffdcd4a810711`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `KeyedConcurrencyAdmission` | 8 |
| `KeyedConcurrencyAdmission.__init__` | 17 |
| `KeyedConcurrencyAdmission.release` | 41 |
| `KeyedConcurrencyAdmission.try_acquire` | 24 |

## `trusted_router.services.lightning`

Frozen alias: `frozen_main.services.lightning`. Member: `src/trusted_router/services/lightning.py`.

SHA-256: `7bde9a938f24a43c5d8e1fe8f4f85ff3ad2b6f93573f0484bc0bc0c93d8ec3cf`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `LightningAccount` | 12 |
| `LightningCredits` | 20 |
| `__create_fn__` | 1 |

## `trusted_router.services.notify`

Frozen alias: `frozen_main.services.notify`. Member: `src/trusted_router/services/notify.py`.

SHA-256: `efe3e3fc9bb93edac522cb07396a1bf0cd241aecea8fa324affc6c12a9f104c1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `NotifyOutcome` | 66 |
| `NotifyService` | 178 |
| `__create_fn__` | 1 |

## `trusted_router.services.ops_chat`

Frozen alias: `frozen_main.services.ops_chat`. Member: `src/trusted_router/services/ops_chat.py`.

SHA-256: `4e81fa3186b8c837c2dcffeaaf7e380131143c2ec6b2ad4cac6e7ec03f7e3832`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `OpsChatFanoutResult` | 23 |
| `OpsChatSupportMessage` | 14 |
| `__create_fn__` | 1 |

## `trusted_router.services.paypal_billing`

Frozen alias: `frozen_main.services.paypal_billing`. Member: `src/trusted_router/services/paypal_billing.py`.

SHA-256: `32dc5487344d7e6630c05d3628fd939c9332615f6f7b50e6b6da378e8c4377cd`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `PayPalCaptureResult` | 54 |
| `_PayPalCreditReference` | 66 |
| `__create_fn__` | 1 |

## `trusted_router.services.receipt_key_collector`

Frozen alias: `frozen_main.services.receipt_key_collector`. Member: `src/trusted_router/services/receipt_key_collector.py`.

SHA-256: `1626e08d5f6d8e9320c41709e7f4ff2758816c53f7abe03cfd23a84771d0f06a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ReceiptKeyTarget` | 39 |
| `__create_fn__` | 1 |

## `trusted_router.services.routable_payouts`

Frozen alias: `frozen_main.services.routable_payouts`. Member: `src/trusted_router/services/routable_payouts.py`.

SHA-256: `f0fe2e47afa4656c18fbbd4b26f8ba30cea125598db12f87b758f62e0a4de7f4`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `RoutableAPIError` | 21 |
| `RoutableClient` | 32 |
| `__create_fn__` | 1 |

## `trusted_router.services.safe_egress`

Frozen alias: `frozen_main.services.safe_egress`. Member: `src/trusted_router/services/safe_egress.py`.

SHA-256: `9608c392b935b4be3937e0388d9f5810d462c00c702d3206f97ba393fa028af0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.ses_suppression`

Frozen alias: `frozen_main.services.ses_suppression`. Member: `src/trusted_router/services/ses_suppression.py`.

SHA-256: `1630d26832c899603c2aa970e789589f026156d2f167b4c85df280d83f3ed3a8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SesSuppressionService` | 16 |
| `SesSuppressionService.__init__` | 19 |
| `SesSuppressionSyncError` | 12 |

## `trusted_router.services.settle_outbox_apply`

Frozen alias: `frozen_main.services.settle_outbox_apply`. Member: `src/trusted_router/services/settle_outbox_apply.py`.

SHA-256: `636a8b6a48ee61bdd0d2f47d3e158aff73ce6da7b9f0e125b9459275e2bcb9de`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ApplyOutcome` | 78 |
| `_apply_typed` | 464 |
| `_frozen_app_markup_payout` | 361 |
| `_frozen_custom_model_markup_payout` | 402 |
| `_frozen_generation` | 290 |
| `_frozen_user_model_payout` | 335 |
| `_parse_settle_body` | 282 |
| `_provider_slug` | 449 |
| `_release_user_model_slot_safely` | 267 |
| `apply_frozen_settle` | 124 |
| `normalized_prompt_accounting` | 101 |

## `trusted_router.services.settle_outbox_drain`

Frozen alias: `frozen_main.services.settle_outbox_drain`. Member: `src/trusted_router/services/settle_outbox_drain.py`.

SHA-256: `6e65f390c91e2f75ad0bb8783afd865240dee9b045645eb9798711b19c6a27c5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_resolve_row` | 191 |
| `drain_settle_outbox` | 68 |
| `spanner_settle_outbox` | 56 |

## `trusted_router.services.stripe_billing`

Frozen alias: `frozen_main.services.stripe_billing`. Member: `src/trusted_router/services/stripe_billing.py`.

SHA-256: `425bebb6e16b1cacea65a82bb9b420c6bada57db3028555b63d91255abb9bee1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.stripe_fees`

Frozen alias: `frozen_main.services.stripe_fees`. Member: `src/trusted_router/services/stripe_fees.py`.

SHA-256: `9172dc527d8b0808591fe936c1ddc43a4a1f931eb81f23bf56037c13a64ba19d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProcessingFee` | 14 |
| `__create_fn__` | 1 |

## `trusted_router.services.telephony`

Frozen alias: `frozen_main.services.telephony`. Member: `src/trusted_router/services/telephony.py`.

SHA-256: `057c4f69989ad219b96434b2a6372c6ff32cc271261a0bec37e75b7c2f139c49`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `TelephonyResult` | 54 |
| `TelephonyService` | 149 |
| `__create_fn__` | 1 |

## `trusted_router.services.trust_release`

Frozen alias: `frozen_main.services.trust_release`. Member: `src/trusted_router/services/trust_release.py`.

SHA-256: `7760b755b404902da0a966b3eedab4f555f0da02e3735d8ad1a72a3949004980`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ResolvedTrustRelease` | 34 |
| `TrustReleaseResolver` | 47 |
| `TrustReleaseResolver.__init__` | 50 |
| `TrustReleaseUnavailable` | 30 |
| `_CacheEntry` | 40 |
| `__create_fn__` | 1 |

## `trusted_router.services.user_model_dispatch`

Frozen alias: `frozen_main.services.user_model_dispatch`. Member: `src/trusted_router/services/user_model_dispatch.py`.

SHA-256: `304b82213a290ffcc6e27706ea2c779354fbacad9f08281693589119ce013f2a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BufferedUserModelDispatch` | 36 |
| `_MalformedOwnerResponse` | 43 |
| `_OwnerFault` | 618 |
| `__create_fn__` | 1 |

## `trusted_router.services.user_model_gateway_health`

Frozen alias: `frozen_main.services.user_model_gateway_health`. Member: `src/trusted_router/services/user_model_gateway_health.py`.

SHA-256: `a1aa3fb5564af6f6156248df6c2803d21dd8cae30575c8be8f0263fd0b583485`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.user_model_probe`

Frozen alias: `frozen_main.services.user_model_probe`. Member: `src/trusted_router/services/user_model_probe.py`.

SHA-256: `67a07c446a44104aa4edcd2adfa6d122e525c07c0c8842a921da186a39c24ec3`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProbeResult` | 30 |
| `__create_fn__` | 1 |

## `trusted_router.services.user_model_secrets`

Frozen alias: `frozen_main.services.user_model_secrets`. Member: `src/trusted_router/services/user_model_secrets.py`.

SHA-256: `ba81a341a9dc1332da7d67f2c5550e0dc70788db16dc76935b56d1d72fc85011`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.user_model_slots`

Frozen alias: `frozen_main.services.user_model_slots`. Member: `src/trusted_router/services/user_model_slots.py`.

SHA-256: `4b224e446240a4bf0ad8db1cee38e1382df2807501efab8542d20d98393850ec`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.services.veriff`

Frozen alias: `frozen_main.services.veriff`. Member: `src/trusted_router/services/veriff.py`.

SHA-256: `c850f6a8060b24225002e7c1dc90d11ab8258d8888c87f4095fba9a43d9aaaf1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `VeriffError` | 15 |
| `VeriffSession` | 19 |
| `__create_fn__` | 1 |

## `trusted_router.services.x402_billing`

Frozen alias: `frozen_main.services.x402_billing`. Member: `src/trusted_router/services/x402_billing.py`.

SHA-256: `d8149188fb562f1abc4692e9a86534b88c297a9baf495367a00cd129938b7e00`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.signup_gate`

Frozen alias: `frozen_main.signup_gate`. Member: `src/trusted_router/signup_gate.py`.

SHA-256: `1fd520ce6f6b31ed107542edee0fc2661f68a82a004fe5816575665d7cbc6257`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.sns_verify`

Frozen alias: `frozen_main.sns_verify`. Member: `src/trusted_router/sns_verify.py`.

SHA-256: `2ef4f2c49f185c158fbe6f76f0655ea8ec53b9cd99f91bdf45a3ddfade02e1fc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SnsVerificationError` | 66 |

## `trusted_router.spend_windows`

Frozen alias: `frozen_main.spend_windows`. Member: `src/trusted_router/spend_windows.py`.

SHA-256: `a8513ffd13637fc1ce05eab88545918d6810f6c310c8bcec1bf4b41479496315`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `KeyLimitExceeded` | 85 |
| `KeyLimitReserveResult` | 62 |
| `KeyWindowLimitDecision` | 44 |
| `KeyWindowLimitExceeded` | 76 |
| `__create_fn__` | 1 |
| `window_floors` | 104 |

## `trusted_router.stage_d`

Frozen alias: `frozen_main.stage_d`. Member: `src/trusted_router/stage_d.py`.

SHA-256: `78f50c2a9ffa5e751e109e3c58baa7dab734d1a7acb56aafe09e26bdcdab9ee5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `billing_pricing_snapshot` | 30 |
| `endpoint_cost_microdollars_from_candidate` | 173 |
| `endpoint_cost_microdollars_from_document` | 233 |
| `endpoint_pricing_candidate` | 95 |
| `endpoint_pricing_candidate.<locals>.rates` | 98 |
| `endpoint_pricing_document` | 139 |
| `pricing_candidate_for_endpoint` | 161 |

## `trusted_router.stage_d_policy`

Frozen alias: `frozen_main.stage_d_policy`. Member: `src/trusted_router/stage_d_policy.py`.

SHA-256: `e8fd6e6b8eca139727e71af4f8062bc097f5a9779bc1ad92abc236b5f38d7ffc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BundleVerifier` | 36 |
| `PolicyWatermark` | 49 |
| `SigstoreBundleVerifier` | 57 |
| `StageDPolicy` | 100 |
| `StageDPolicyResolver` | 175 |
| `StageDPolicyResolver.__init__` | 178 |
| `StorePolicyWatermark` | 80 |
| `StorePolicyWatermark.__init__` | 83 |
| `__create_fn__` | 1 |

## `trusted_router.storage`

Frozen alias: `frozen_main.storage`. Member: `src/trusted_router/storage.py`.

SHA-256: `6b3102183d2d946dabc1691043c02aaa0a9a1ea8a4e6961bdcedb20edcbb620d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryStore` | 148 |
| `InMemoryStore.__init__` | 156 |
| `_StoreProxy` | 3812 |
| `_StoreProxy.__getattr__` | 3868 |
| `_StoreProxy.__init__` | 3828 |
| `_StoreProxy._configure` | 3832 |
| `_StoreProxy.target` | 3836 |
| `configure_analytics_sink` | 3902 |
| `configure_store` | 3882 |
| `create_store` | 3908 |
| `typed_billing_store` | 3886 |

## `trusted_router.storage_activity`

Frozen alias: `frozen_main.storage_activity`. Member: `src/trusted_router/storage_activity.py`.

SHA-256: `e94dbf7230793f7ef91b2b46995f0a8e0e922f72c8a36a2c6d983a3eb4497379`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ActivityResult` | 12 |
| `__create_fn__` | 1 |

## `trusted_router.storage_attribution`

Frozen alias: `frozen_main.storage_attribution`. Member: `src/trusted_router/storage_attribution.py`.

SHA-256: `0d9cfd005d3ca4fac14477dd92c4ffb8679502c212e2d525c46186187e8e6f77`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryAcquisitionAttribution` | 17 |
| `InMemoryAcquisitionAttribution.__init__` | 18 |

## `trusted_router.storage_auth_context`

Frozen alias: `frozen_main.storage_auth_context`. Member: `src/trusted_router/storage_auth_context.py`.

SHA-256: `205afd6ed1728525de23f04323a8f1361589cf8d9db0f39afdbdc0e331b4eae1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.storage_auth_sessions`

Frozen alias: `frozen_main.storage_auth_sessions`. Member: `src/trusted_router/storage_auth_sessions.py`.

SHA-256: `695bbbee22ba7aea70d98cf2b6b4e9e4a441537719d13f7033beb62d46ce7745`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryAuthSessions` | 24 |
| `InMemoryAuthSessions.__init__` | 25 |

## `trusted_router.storage_broadcast`

Frozen alias: `frozen_main.storage_broadcast`. Member: `src/trusted_router/storage_broadcast.py`.

SHA-256: `3f2c884e7dc46e99e92b467fd33ee38fda1f1f6f654bab2aeaa8520e843e2d4c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryBroadcastDestinations` | 15 |
| `InMemoryBroadcastDestinations.__init__` | 16 |

## `trusted_router.storage_byok`

Frozen alias: `frozen_main.storage_byok`. Member: `src/trusted_router/storage_byok.py`.

SHA-256: `60b359fd83cc74e48b4a4199750f57e4b972d03472254a653f03c66552be22c3`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryByok` | 19 |
| `InMemoryByok.__init__` | 20 |

## `trusted_router.storage_codec`

Frozen alias: `frozen_main.storage_codec`. Member: `src/trusted_router/storage_codec.py`.

SHA-256: `954889a6b1e798c187cdf0b918431fc8b3e74d4536dbe9ac1029edf6881d5025`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `json_body` | 25 |

## `trusted_router.storage_custom_models`

Frozen alias: `frozen_main.storage_custom_models`. Member: `src/trusted_router/storage_custom_models.py`.

SHA-256: `8692bf3038ff3c67d3b8d6688177522d9f8cebf4e7f77df2bb6b2767462a5d1c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryCustomModels` | 24 |
| `InMemoryCustomModels.__init__` | 25 |

## `trusted_router.storage_email_blocks`

Frozen alias: `frozen_main.storage_email_blocks`. Member: `src/trusted_router/storage_email_blocks.py`.

SHA-256: `bc579ce29805325926a87f4cc54f6edcdacd449bf91d088137cdb6b576e3be34`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryEmailBlocks` | 16 |
| `InMemoryEmailBlocks.__init__` | 22 |

## `trusted_router.storage_errors`

Frozen alias: `frozen_main.storage_errors`. Member: `src/trusted_router/storage_errors.py`.

SHA-256: `ad1467651d4fb92e9d34973ea8287c9daccb5e4d4052710aea07aaa80a323e07`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `DeferredSettlementCapReached` | 55 |
| `StoreConflict` | 36 |
| `StoreError` | 32 |
| `StoreUnavailable` | 46 |
| `_google_error_types` | 71 |
| `_postgres_error_types` | 109 |
| `conflict_store_error_types` | 146 |
| `transient_store_error_types` | 129 |

## `trusted_router.storage_gcp`

Frozen alias: `frozen_main.storage_gcp`. Member: `src/trusted_router/storage_gcp.py`.

SHA-256: `15323301fe6b5d92f3383ce80585fe9efa8da6517b4a47999938d515938818a8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerStore` | 385 |
| `SpannerStore._list_entities` | 6967 |
| `SpannerStore._read_entity` | 6921 |
| `SpannerStore._read_entity_from` | 6941 |
| `SpannerStore.claim_broadcast_deliveries` | 3015 |
| `SpannerStore.get_acquisition_attribution` | 604 |
| `SpannerStore.get_credit_account` | 1838 |
| `SpannerStore.get_gateway_authorization` | 4709 |
| `SpannerStore.get_key_by_hash` | 2478 |
| `SpannerStore.list_broadcast_destinations` | 2978 |
| `SpannerStore.reap_expired_reservations_result` | 5658 |
| `SpannerStore.typed_finalize_gateway` | 4774 |
| `SpannerStore.typed_settle_one_commit_result` | 4953 |
| `SpannerStore.typed_settle_one_commit_result.<locals>.attempt` | 5040 |
| `_AuthorizationReplay` | 239 |

## `trusted_router.storage_gcp_analytics_outbox`

Frozen alias: `frozen_main.storage_gcp_analytics_outbox`. Member: `src/trusted_router/storage_gcp_analytics_outbox.py`.

SHA-256: `8c546064fce8e036776669cf5d456acd13c206bf1709f0d7a0ac43be03eae5a2`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerAnalyticsOutbox` | 39 |
| `SpannerAnalyticsOutbox.__init__` | 42 |
| `SpannerAnalyticsOutbox.enqueue` | 55 |
| `SpannerAnalyticsOutbox.enqueue.<locals>.txn` | 62 |
| `SpannerAnalyticsOutbox.enqueue_statement` | 72 |
| `SpannerAnalyticsOutbox.enqueue_tx` | 67 |
| `analytics_outbox_shard` | 31 |

## `trusted_router.storage_gcp_async_admission`

Frozen alias: `frozen_main.storage_gcp_async_admission`. Member: `src/trusted_router/storage_gcp_async_admission.py`.

SHA-256: `62810d8ac37f54d9cc17e55009bf16668e9ced89005bf42d00bf5b1270442f6f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.storage_gcp_async_settle`

Frozen alias: `frozen_main.storage_gcp_async_settle`. Member: `src/trusted_router/storage_gcp_async_settle.py`.

SHA-256: `7aca516fdc555fb73ac978b49d78c2304da867deed2cd7d9d1519142a2e61e4c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ReservationNotOpen` | 24 |

## `trusted_router.storage_gcp_attribution`

Frozen alias: `frozen_main.storage_gcp_attribution`. Member: `src/trusted_router/storage_gcp_attribution.py`.

SHA-256: `ea96da93661b6fc994b0a277474e75473f9ca890d66d118183f4e6858885f73f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerAcquisitionAttribution` | 29 |
| `SpannerAcquisitionAttribution.__init__` | 30 |
| `SpannerAcquisitionAttribution.get` | 122 |

## `trusted_router.storage_gcp_auth_sessions`

Frozen alias: `frozen_main.storage_gcp_auth_sessions`. Member: `src/trusted_router/storage_gcp_auth_sessions.py`.

SHA-256: `4b5d0a41d45e7f8e43d351a368dda5f56d079917f9fc5a39d8ed05119997a702`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerAuthSessions` | 19 |
| `SpannerAuthSessions.__init__` | 20 |

## `trusted_router.storage_gcp_authorize`

Frozen alias: `frozen_main.storage_gcp_authorize`. Member: `src/trusted_router/storage_gcp_authorize.py`.

SHA-256: `f30047ae534e55bdc11a3957bd09b436dc5f6f2bd4b5514558a534d75d59c9fe`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AuthorizeOutcome` | 137 |
| `AuthorizeVerdict` | 147 |
| `ExhaustedKeyCache` | 201 |
| `ExhaustedKeyCache.__init__` | 204 |
| `OneCommitSettleDeclined` | 820 |
| `ReapPassResult` | 835 |
| `ReapPassResult.__init__` | 2 |
| `ReapPassResult.out_of_cohort_share` | 863 |
| `ReapPassResult.started_marker_share` | 859 |
| `SettleOutcome` | 800 |
| `_ReapGuardLost` | 816 |
| `_ReapOneResult` | 868 |
| `_Reject` | 163 |
| `_RetrySequentialCreditReserve` | 170 |
| `_RetrySequentialFinalize` | 187 |
| `_RetrySequentialKeyReserve` | 177 |
| `_SettleError` | 811 |
| `__create_fn__` | 1 |
| `_cached_outbox_availability` | 1129 |
| `_log_missing_key_releases` | 938 |
| `_outbox_cache_key` | 1108 |
| `_outbox_table_available` | 1158 |
| `_release_key_or_skip_deleted` | 878 |
| `_remember_outbox_availability` | 1144 |
| `reap_expired_reservations_result` | 1224 |
| `typed_finalize_atomic` | 1692 |
| `typed_finalize_atomic.<locals>.run` | 2095 |
| `typed_finalize_atomic.<locals>.run.<locals>.tracked` | 2102 |
| `typed_finalize_atomic.<locals>.speculative_batch` | 1801 |
| `typed_finalize_atomic.<locals>.speculative_batch.<locals>.<genexpr>` | 1891 |
| `typed_finalize_atomic.<locals>.speculative_batch.<locals>.check_prefix` | 1873 |
| `typed_finalize_atomic.<locals>.txn` | 1894 |

## `trusted_router.storage_gcp_batch_dml`

Frozen alias: `frozen_main.storage_gcp_batch_dml`. Member: `src/trusted_router/storage_gcp_batch_dml.py`.

SHA-256: `0bc5f9e3161e869c37d70df0ff3e3d6bf2d247a54f42fe6a6968ae9dc9fd8e2a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_BatchDmlAbortCause` | 16 |
| `execute_batch_dml` | 31 |
| `execute_batch_dml.<locals>.<genexpr>` | 60 |

## `trusted_router.storage_gcp_broadcast`

Frozen alias: `frozen_main.storage_gcp_broadcast`. Member: `src/trusted_router/storage_gcp_broadcast.py`.

SHA-256: `a85339557a1321155c976e4e29b05479b1e1a566f8893f28082f1e8076071ad1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerBroadcastDestinations` | 16 |
| `SpannerBroadcastDestinations.__init__` | 17 |
| `SpannerBroadcastDestinations.claim_deliveries` | 170 |
| `SpannerBroadcastDestinations.due_deliveries` | 147 |
| `SpannerBroadcastDestinations.list_for_workspace` | 55 |
| `_iso_after_seconds` | 281 |

## `trusted_router.storage_gcp_byok`

Frozen alias: `frozen_main.storage_gcp_byok`. Member: `src/trusted_router/storage_gcp_byok.py`.

SHA-256: `69d07625b4063e045491ab85b7e42810c62074a3330599d28c975020951dbebe`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerByok` | 18 |
| `SpannerByok.__init__` | 19 |

## `trusted_router.storage_gcp_codec`

Frozen alias: `frozen_main.storage_gcp_codec`. Member: `src/trusted_router/storage_gcp_codec.py`.

SHA-256: `2060d9b39f0645b8937a5aae7e4f4fec16054d08de07428cd34c10df5bee8531`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `generation_workspace_id` | 36 |

## `trusted_router.storage_gcp_counter_dml`

Frozen alias: `frozen_main.storage_gcp_counter_dml`. Member: `src/trusted_router/storage_gcp_counter_dml.py`.

SHA-256: `b262f1ecb008a0cc84025fa84b139776c7d964e4b614b636c878731d5573f164`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `claim_reservation_statement` | 810 |
| `read_reservation` | 723 |
| `release_credit` | 299 |
| `release_credit.<locals>.absorb` | 358 |
| `release_credit_no_debt_statement` | 409 |
| `release_key` | 622 |
| `release_key_statement` | 550 |
| `reservation_retention_clear_statement` | 907 |

## `trusted_router.storage_gcp_counters`

Frozen alias: `frozen_main.storage_gcp_counters`. Member: `src/trusted_router/storage_gcp_counters.py`.

SHA-256: `38e8845cd529c485ee67691dff6413f3f25328801dcd6b508b5cfc4363bcbf15`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.storage_gcp_credit_debt`

Frozen alias: `frozen_main.storage_gcp_credit_debt`. Member: `src/trusted_router/storage_gcp_credit_debt.py`.

SHA-256: `3098d0c1eb4f7488dae52e6ea0c96cff3b80ef20929cbe6696dc502ccb35ea89`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `CreditRows` | 56 |
| `CreditRowsChanged` | 41 |
| `CreditRowsIncomplete` | 45 |
| `InflowResult` | 187 |
| `__create_fn__` | 1 |

## `trusted_router.storage_gcp_credit_shards`

Frozen alias: `frozen_main.storage_gcp_credit_shards`. Member: `src/trusted_router/storage_gcp_credit_shards.py`.

SHA-256: `084fb0ec657a057b5dd985ddf0b1d7f7e9ba9dd3b939a97b2bc5fd41a51d6c1a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `CreditShardConfigurationMissingError` | 25 |
| `CreditShardCountCache` | 54 |
| `CreditShardCountCache.__init__` | 66 |
| `CreditShardCountCache.__init__.<locals>.<genexpr>` | 82 |
| `_CacheEntry` | 47 |
| `__create_fn__` | 1 |

## `trusted_router.storage_gcp_credit_transfer`

Frozen alias: `frozen_main.storage_gcp_credit_transfer`. Member: `src/trusted_router/storage_gcp_credit_transfer.py`.

SHA-256: `20cdfd3b81f9fb20ff6244a14a19ee661488468d20703e89c91d68d1cbe86b6f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `EscrowPlanError` | 91 |

## `trusted_router.storage_gcp_custom_models`

Frozen alias: `frozen_main.storage_gcp_custom_models`. Member: `src/trusted_router/storage_gcp_custom_models.py`.

SHA-256: `a02b231b60e0ad2c0651dbf1b60228f35e17e014ac54b87123e474661d11b213`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerCustomModels` | 26 |
| `SpannerCustomModels.__init__` | 27 |

## `trusted_router.storage_gcp_email_blocks`

Frozen alias: `frozen_main.storage_gcp_email_blocks`. Member: `src/trusted_router/storage_gcp_email_blocks.py`.

SHA-256: `e409852ad5cd88805bd5a9191918d722c5b3e497fdc19b1b46e24050c2a21be8`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerEmailBlocks` | 15 |
| `SpannerEmailBlocks.__init__` | 16 |

## `trusted_router.storage_gcp_generation_records`

Frozen alias: `frozen_main.storage_gcp_generation_records`. Member: `src/trusted_router/storage_gcp_generation_records.py`.

SHA-256: `3d78d071d123b78a65f0baa77f6217dec78aa5207ec6e3634e7fa47432b0e8b5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_timestamp` | 122 |
| `generation_insert_statement` | 40 |
| `generation_record_body` | 22 |

## `trusted_router.storage_gcp_generations`

Frozen alias: `frozen_main.storage_gcp_generations`. Member: `src/trusted_router/storage_gcp_generations.py`.

SHA-256: `f5a652f450dd62098ffc34dd2c567a4a08a768ac08a77d6c911def759962508e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ActivityReconcileResult` | 48 |
| `SpannerGenerations` | 62 |
| `SpannerGenerations.__init__` | 63 |
| `SpannerGenerations.analytics_outbox` | 117 |
| `SpannerGenerations.benchmark_sample` | 122 |
| `SpannerGenerations.post_commit_analytics` | 133 |
| `SpannerGenerations.record_benchmark` | 211 |
| `_AddUsageCallback` | 58 |
| `__create_fn__` | 1 |

## `trusted_router.storage_gcp_group_buy`

Frozen alias: `frozen_main.storage_gcp_group_buy`. Member: `src/trusted_router/storage_gcp_group_buy.py`.

SHA-256: `370a443bbcfe686020dbd508e8ad674cdee0bfd87569c8c9068052b2460b288c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerBedrockGroupBuy` | 27 |
| `SpannerBedrockGroupBuy.__init__` | 28 |

## `trusted_router.storage_gcp_io`

Frozen alias: `frozen_main.storage_gcp_io`. Member: `src/trusted_router/storage_gcp_io.py`.

SHA-256: `bc03206cc99c37f653989d2fa11e82b5be974e51e871a358a6466f26731bf008`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerIO` | 451 |
| `SpannerIO.__init__` | 2 |
| `SpannerRpcCounter` | 54 |
| `SpannerRpcCounter.__init__` | 2 |
| `SpannerRpcCounter.value` | 69 |
| `__create_fn__` | 1 |
| `_rollback_on_api_error` | 393 |
| `_rollback_on_api_error.<locals>.rolled_back` | 410 |
| `count_spanner_rpcs` | 79 |
| `default_transaction_tag` | 377 |
| `default_transaction_tag.<locals>.<genexpr>` | 390 |
| `remaining_rpc_budget` | 136 |
| `run_in_transaction_with_retry` | 279 |
| `spanner_rpc_budget` | 113 |
| `spanner_rpc_budget.<locals>.decorate` | 118 |
| `spanner_rpc_budget.<locals>.decorate.<locals>.budgeted` | 119 |
| `spanner_rpc_deadline` | 94 |

## `trusted_router.storage_gcp_keys`

Frozen alias: `frozen_main.storage_gcp_keys`. Member: `src/trusted_router/storage_gcp_keys.py`.

SHA-256: `28956fe72544b2379ec7f2ee5794acd2fc9b18660ae6ab1c060eabd6cbd6b058`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerApiKeys` | 88 |
| `SpannerApiKeys.__init__` | 89 |
| `SpannerApiKeys.get_by_hash` | 164 |

## `trusted_router.storage_gcp_oauth_apps`

Frozen alias: `frozen_main.storage_gcp_oauth_apps`. Member: `src/trusted_router/storage_gcp_oauth_apps.py`.

SHA-256: `07c3f956633fd042bb32ee856a7eaa5399a8f24395446db66ebccbc7369b33ec`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerOAuthApps` | 10 |
| `SpannerOAuthApps.__init__` | 11 |

## `trusted_router.storage_gcp_oauth_codes`

Frozen alias: `frozen_main.storage_gcp_oauth_codes`. Member: `src/trusted_router/storage_gcp_oauth_codes.py`.

SHA-256: `d2bacc72b87da09fe27b261c07352e6aeb343cfdd013be204be9c1bab90039c6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerOAuthCodes` | 29 |
| `SpannerOAuthCodes.__init__` | 30 |

## `trusted_router.storage_gcp_operational_analytics_outbox`

Frozen alias: `frozen_main.storage_gcp_operational_analytics_outbox`. Member: `src/trusted_router/storage_gcp_operational_analytics_outbox.py`.

SHA-256: `2ea7a84b07577870afd497b5561a44895390e7c3df2a49abe1c27acfa0a9cb42`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerOperationalAnalyticsOutbox` | 48 |
| `SpannerOperationalAnalyticsOutbox.__init__` | 51 |
| `SpannerOperationalAnalyticsOutbox._insert_statement` | 237 |
| `SpannerOperationalAnalyticsOutbox.activity_insert_statement` | 86 |

## `trusted_router.storage_gcp_rate_limits`

Frozen alias: `frozen_main.storage_gcp_rate_limits`. Member: `src/trusted_router/storage_gcp_rate_limits.py`.

SHA-256: `24e3d202185aa025618fa03c2eac5af49cfceda0bbc5c533297ab4167a3fb054`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerRateLimits` | 12 |
| `SpannerRateLimits.__init__` | 13 |

## `trusted_router.storage_gcp_request_records`

Frozen alias: `frozen_main.storage_gcp_request_records`. Member: `src/trusted_router/storage_gcp_request_records.py`.

SHA-256: `0d768b096a8a9d3ab60115c28104c6eb65a69da4cbf45b476bde43261738f724`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AuthorizationDataError` | 57 |
| `_authorization_from_payload` | 461 |
| `_current_heartbeat_json_sql` | 297 |
| `_settled_payload_sql` | 315 |
| `_timestamp_string` | 467 |
| `authorization_typed_columns` | 61 |
| `gateway_authorization_retention_clear_statement` | 449 |
| `gateway_authorization_settled_statement` | 345 |
| `gateway_authorization_settled_statement.<locals>.<genexpr>` | 364 |
| `merge_authorization_typed_columns` | 93 |
| `read_gateway_authorization` | 212 |

## `trusted_router.storage_gcp_settle_outbox`

Frozen alias: `frozen_main.storage_gcp_settle_outbox`. Member: `src/trusted_router/storage_gcp_settle_outbox.py`.

SHA-256: `eb0532fb27eb95968d9ec7e20ed3c79ecb5e52d880f1f1a699ad66c2824f0a14`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 84 |
| `<module>` | 1 |
| `SpannerSettleOutbox` | 491 |
| `SpannerSettleOutbox.__init__` | 494 |
| `SpannerSettleOutbox._claim_one` | 655 |
| `SpannerSettleOutbox._claim_one.<locals>.txn` | 658 |
| `SpannerSettleOutbox.claim` | 627 |
| `SpannerSettleOutbox.due` | 609 |
| `SpannerSettleOutbox.enqueue` | 500 |
| `SpannerSettleOutbox.enqueue.<locals>.insert_txn` | 515 |
| `SpannerSettleOutbox.get` | 1119 |
| `SpannerSettleOutbox.mark` | 681 |
| `SpannerSettleOutbox.mark.<locals>.txn` | 706 |
| `SpannerSettleOutbox.purge_done` | 1131 |
| `_iso_after_seconds` | 427 |
| `_mark_done_tx` | 382 |
| `_resolve_done_retention_tx` | 111 |
| `_row_from_tuple` | 439 |
| `_ts_str` | 482 |
| `done_retention_statements` | 141 |
| `intent_insert_counts` | 284 |
| `intent_insert_statements` | 178 |
| `intent_insert_statements.<locals>.<genexpr>` | 205 |
| `resolved_intent_statements` | 289 |

## `trusted_router.storage_gcp_stage_d`

Frozen alias: `frozen_main.storage_gcp_stage_d`. Member: `src/trusted_router/storage_gcp_stage_d.py`.

SHA-256: `429ab16d053f0a6916313bc1861e33fcf52786148b029740c1a6fb9a873b43d1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `HeartbeatResult` | 32 |
| `_RollbackHeartbeat` | 43 |
| `__create_fn__` | 1 |

## `trusted_router.storage_gcp_trust`

Frozen alias: `frozen_main.storage_gcp_trust`. Member: `src/trusted_router/storage_gcp_trust.py`.

SHA-256: `51ea1a5884501c89a58a25f15a0e20497ff9a3a028acf15f486acd0a0181f6f1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `RecordingReader` | 817 |
| `absorb_unrecovered_recovery_tx` | 259 |

## `trusted_router.storage_gcp_user_models`

Frozen alias: `frozen_main.storage_gcp_user_models`. Member: `src/trusted_router/storage_gcp_user_models.py`.

SHA-256: `c1ad40ccc4b59dd563045def36be4f1f548336e316b2ab5acf9155f7ff76393d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerUserProvidedModels` | 51 |
| `SpannerUserProvidedModels.__init__` | 52 |

## `trusted_router.storage_gcp_verification_tokens`

Frozen alias: `frozen_main.storage_gcp_verification_tokens`. Member: `src/trusted_router/storage_gcp_verification_tokens.py`.

SHA-256: `768c5c724b0290c0b7df2337f3b7f8bf95c8461ca091d1e4ed089617461a7f1e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerVerificationTokens` | 23 |
| `SpannerVerificationTokens.__init__` | 24 |

## `trusted_router.storage_gcp_video_jobs`

Frozen alias: `frozen_main.storage_gcp_video_jobs`. Member: `src/trusted_router/storage_gcp_video_jobs.py`.

SHA-256: `b6301a1487475f7fa23e303110ed1e4a501e61e489ce41c18a9e57b02e8c3f23`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerVideoJobs` | 14 |
| `SpannerVideoJobs.__init__` | 23 |

## `trusted_router.storage_gcp_wallet_challenges`

Frozen alias: `frozen_main.storage_gcp_wallet_challenges`. Member: `src/trusted_router/storage_gcp_wallet_challenges.py`.

SHA-256: `47d27e2c534ff3d1c96779a56ae2faeab4f2e1c3b3adc17c1e0e86615525bd85`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SpannerWalletChallenges` | 33 |
| `SpannerWalletChallenges.__init__` | 34 |

## `trusted_router.storage_generations`

Frozen alias: `frozen_main.storage_generations`. Member: `src/trusted_router/storage_generations.py`.

SHA-256: `18d342478a3d17df196a58de29d6b348577c26c339255e7178d63671aa7454d1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryGenerations` | 34 |
| `InMemoryGenerations.__init__` | 35 |
| `_AddUsageCallback` | 30 |

## `trusted_router.storage_group_buy`

Frozen alias: `frozen_main.storage_group_buy`. Member: `src/trusted_router/storage_group_buy.py`.

SHA-256: `b8e61a1f4a0084359e6b1987bb22f70078a11f40be13279f5ce7ba85a605a75d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryBedrockGroupBuy` | 159 |
| `InMemoryBedrockGroupBuy.__init__` | 160 |
| `PreparedPledgeMutation` | 81 |
| `__create_fn__` | 1 |

## `trusted_router.storage_key_patch`

Frozen alias: `frozen_main.storage_key_patch`. Member: `src/trusted_router/storage_key_patch.py`.

SHA-256: `7fbc2dcd015287738441a77616f03134d1b1eeb9a64f33be930c774b6ed209d9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.storage_key_usage`

Frozen alias: `frozen_main.storage_key_usage`. Member: `src/trusted_router/storage_key_usage.py`.

SHA-256: `95d9d0d88e1854450c0411f99e4a63bba4ee70fd1e940aa5ec62fa84bcfb1f15`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.storage_keys`

Frozen alias: `frozen_main.storage_keys`. Member: `src/trusted_router/storage_keys.py`.

SHA-256: `cef192053ae10aa0d8cf0cf39be92cc22a252d4ee4b6c8092136e851a97a69c4`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryApiKeys` | 62 |
| `InMemoryApiKeys.__init__` | 63 |

## `trusted_router.storage_legacy_trust`

Frozen alias: `frozen_main.storage_legacy_trust`. Member: `src/trusted_router/storage_legacy_trust.py`.

SHA-256: `3f700a2cbc40d4cc078455b6fd53f710862b09961460f5b5cbc6b62a4e08a531`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BillingPausedError` | 17 |

## `trusted_router.storage_lightning`

Frozen alias: `frozen_main.storage_lightning`. Member: `src/trusted_router/storage_lightning.py`.

SHA-256: `2d612f24589e6167631b6e9c054ba28476a0d1503e0b0d839fbdff534c6cd4cb`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 34 |
| `<module>` | 1 |

## `trusted_router.storage_models`

Frozen alias: `frozen_main.storage_models`. Member: `src/trusted_router/storage_models.py`.

SHA-256: `cab2350e32369f73ae25ee288a8a290620e91cdac8ebe80a0074c349d8efde61`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `AcquisitionAttribution` | 1700 |
| `ActivationReminderTask` | 1774 |
| `AdverseTrustEvent` | 688 |
| `AdverseTrustResult` | 705 |
| `AmbiguousGatewayRequestId` | 25 |
| `ApiKey` | 287 |
| `ApiKey.__init__` | 2 |
| `ApiKeyAuthContext` | 1920 |
| `ApiKeyUsageSnapshot` | 361 |
| `AppMarkupPayout` | 982 |
| `AuthSession` | 1875 |
| `AutoRefillOutboxRow` | 606 |
| `BedrockGroupBuyAggregate` | 1855 |
| `BedrockGroupBuyAggregateShard` | 1843 |
| `BedrockGroupBuyPledge` | 1808 |
| `BedrockGroupBuyPublicMessage` | 1865 |
| `BroadcastDeliveryJob` | 502 |
| `BroadcastDestination` | 479 |
| `ByokProviderConfig` | 403 |
| `ConsentRequest` | 2013 |
| `CreditAccount` | 623 |
| `CreditAccount.__init__` | 2 |
| `CreditMoney` | 723 |
| `CreditMovement` | 734 |
| `CreditProvenance` | 645 |
| `CreditTransfer` | 2047 |
| `CustomModel` | 418 |
| `CustomModelMarkupPayout` | 990 |
| `EarningsCashout` | 766 |
| `EmailSendBlock` | 1934 |
| `EncryptedGoogleClickEnvelope` | 391 |
| `EncryptedSecretEnvelope` | 381 |
| `GatewayAuthorization` | 810 |
| `GatewayAuthorization.__init__` | 2 |
| `GatewayAuthorization.__post_init__` | 910 |
| `GatewayAuthorization.record_finalization` | 927 |
| `GatewayBoot` | 1903 |
| `Generation` | 998 |
| `Generation.__init__` | 2 |
| `Generation.__post_init__` | 1069 |
| `Generation.from_settle_body` | 1172 |
| `GoogleAdsConversion` | 1734 |
| `Member` | 279 |
| `OAuthApp` | 348 |
| `OAuthAuthorizationCode` | 1984 |
| `ProviderAccessGrant` | 197 |
| `ProviderBenchmarkSample` | 1381 |
| `ProviderBenchmarkSample.__init__` | 2 |
| `ProviderBenchmarkSample.__post_init__` | 1441 |
| `ProviderBenchmarkSample.from_generation` | 1445 |
| `ProviderBenchmarkSample.from_provider_error` | 1507 |
| `RateLimitHit` | 2038 |
| `RateLimitHit.__init__` | 2 |
| `ReceiptKey` | 120 |
| `Reservation` | 790 |
| `RoutablePayoutProfile` | 746 |
| `SessionAuthContext` | 1890 |
| `SettleOutboxRow` | 561 |
| `SettleOutboxRow.__init__` | 2 |
| `SignupResult` | 1689 |
| `SyntheticProbeSample` | 1563 |
| `SyntheticRollup` | 1631 |
| `TrustEvent` | 664 |
| `TrustInboxRow` | 715 |
| `TrustOverride` | 260 |
| `TypedFinalizeResult` | 963 |
| `TypedFinalizeResult.__init__` | 2 |
| `User` | 143 |
| `UserModelPayout` | 974 |
| `UserProvidedModel` | 435 |
| `VerificationToken` | 1969 |
| `VideoJob` | 519 |
| `WalletChallenge` | 1954 |
| `Workspace` | 223 |
| `__create_fn__` | 1 |
| `_is_synthetic_metadata` | 69 |
| `_seconds_to_milliseconds` | 1545 |
| `generation_id_for_authorization` | 1358 |
| `iso_now` | 33 |
| `resolve_synthetic` | 78 |
| `utcnow` | 29 |

## `trusted_router.storage_oauth_apps`

Frozen alias: `frozen_main.storage_oauth_apps`. Member: `src/trusted_router/storage_oauth_apps.py`.

SHA-256: `55899347706aaf93a72dbe34cd8b9078f8c1d6c0022fefa09fe51a141f9029b9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryOAuthApps` | 35 |
| `InMemoryOAuthApps.__init__` | 36 |

## `trusted_router.storage_oauth_codes`

Frozen alias: `frozen_main.storage_oauth_codes`. Member: `src/trusted_router/storage_oauth_codes.py`.

SHA-256: `342accdd3d8e7649ef2fdfecc26d5be6d6c759cd1c0d7fdb062607d84fb0d27d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryOAuthCodes` | 28 |
| `InMemoryOAuthCodes.__init__` | 29 |

## `trusted_router.storage_operational_analytics`

Frozen alias: `frozen_main.storage_operational_analytics`. Member: `src/trusted_router/storage_operational_analytics.py`.

SHA-256: `195a1a50de1bfb7c888d22323e00d9c03289d0bdde806469ec0de67c6fdc47e2`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `OperationalAnalyticsWriter` | 34 |
| `activity_payload` | 185 |
| `analytics_surrogate` | 67 |
| `operational_analytics_shard` | 56 |

## `trusted_router.storage_rate_limits`

Frozen alias: `frozen_main.storage_rate_limits`. Member: `src/trusted_router/storage_rate_limits.py`.

SHA-256: `68af0a2637fe3f4c822d4694832556b0f22a85d3083d0626c0a4fb1c53bcf514`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryRateLimits` | 27 |
| `InMemoryRateLimits.__init__` | 28 |
| `InMemoryRateLimits.hit` | 48 |

## `trusted_router.storage_synthetic`

Frozen alias: `frozen_main.storage_synthetic`. Member: `src/trusted_router/storage_synthetic.py`.

SHA-256: `e5dd845d3cde99862e1201f7c8218a7d4750e11032f00fcbf72f564ccd6b7d02`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemorySyntheticChecks` | 17 |
| `InMemorySyntheticChecks.__init__` | 18 |

## `trusted_router.storage_user_models`

Frozen alias: `frozen_main.storage_user_models`. Member: `src/trusted_router/storage_user_models.py`.

SHA-256: `29a446ab001cf5ff515c8411e187893972990fba6ecd413e17e29dc97c6e0f3f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryUserProvidedModels` | 46 |
| `InMemoryUserProvidedModels.__init__` | 47 |

## `trusted_router.storage_verification_tokens`

Frozen alias: `frozen_main.storage_verification_tokens`. Member: `src/trusted_router/storage_verification_tokens.py`.

SHA-256: `31240d4d6d9bad54f4341090ec37e85d3012622bc1e3afd6143578f520da7ddf`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryVerificationTokens` | 20 |
| `InMemoryVerificationTokens.__init__` | 21 |

## `trusted_router.storage_video_jobs`

Frozen alias: `frozen_main.storage_video_jobs`. Member: `src/trusted_router/storage_video_jobs.py`.

SHA-256: `e8056ca835d64a19f0e7db9b10a74a33c787aa1f92ed42312d4ba2670177634c`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryVideoJobs` | 12 |
| `InMemoryVideoJobs.__init__` | 13 |

## `trusted_router.storage_wallet_challenges`

Frozen alias: `frozen_main.storage_wallet_challenges`. Member: `src/trusted_router/storage_wallet_challenges.py`.

SHA-256: `16ac2ee83fb3a8503dc010431e6be38207391761363d827c5d7842a28005097a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `InMemoryWalletChallenges` | 88 |
| `InMemoryWalletChallenges.__init__` | 89 |

## `trusted_router.store_protocol`

Frozen alias: `frozen_main.store_protocol`. Member: `src/trusted_router/store_protocol.py`.

SHA-256: `85cd2a8df41f89c7b9e6565b6f980d0b8520265a53939a60d1b35b47a530fc8d`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ShadowStore` | 1242 |
| `ShadowTransaction` | 1237 |
| `SnapshotReaperStore` | 1224 |
| `Store` | 74 |
| `TypedBillingStore` | 1131 |

## `trusted_router.synthetic`

Frozen alias: `frozen_main.synthetic`. Member: `src/trusted_router/synthetic/__init__.py`.

SHA-256: `d2a06ce18138fc7e19c3dd064660aa85e5d25d31fcbf92c3ccc714490086a156`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.alerts`

Frozen alias: `frozen_main.synthetic.alerts`. Member: `src/trusted_router/synthetic/alerts.py`.

SHA-256: `4dd9f312dbacb25974a3f739a046a0fceca2c1af0638288b7d504f0b207fd3a1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.cli`

Frozen alias: `frozen_main.synthetic.cli`. Member: `src/trusted_router/synthetic/cli.py`.

SHA-256: `f64c83dce5e3874b3e11fe4d7164b2ca2b104a99eab505f1a6cd7e1e33656ba5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.client_watch`

Frozen alias: `frozen_main.synthetic.client_watch`. Member: `src/trusted_router/synthetic/client_watch.py`.

SHA-256: `1f18eb0201a3311fd5a96b08f22499d1d2ac5a179f3a05fb017539f50973e1ea`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ClientWatchAlert` | 14 |
| `__create_fn__` | 1 |

## `trusted_router.synthetic.components`

Frozen alias: `frozen_main.synthetic.components`. Member: `src/trusted_router/synthetic/components.py`.

SHA-256: `035f6ba0e555e167f29e118968ed9d35b67dfbca1f4adb43bf19b69852dcf0b6`.

| Callable / executed code unit | Code line |
|---|---|
| `<genexpr>` | 307 |
| `<module>` | 1 |

## `trusted_router.synthetic.fleet`

Frozen alias: `frozen_main.synthetic.fleet`. Member: `src/trusted_router/synthetic/fleet.py`.

SHA-256: `67a0c77043a4a0d699ec64d073508591af095ac7fd17d35e601049fe15862c18`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.funding`

Frozen alias: `frozen_main.synthetic.funding`. Member: `src/trusted_router/synthetic/funding.py`.

SHA-256: `1ba3838ee6d24a40d34fc804ac605dc1b62123dc58ad337d99d4561f1a8a9baf`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.inference_sdk`

Frozen alias: `frozen_main.synthetic.inference_sdk`. Member: `src/trusted_router/synthetic/inference_sdk.py`.

SHA-256: `1b8973ff33bb27871ca4c4f823603fb0102196835d8ee58175284f8db94c301e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SdkFailure` | 95 |
| `__create_fn__` | 1 |

## `trusted_router.synthetic.internal_auth`

Frozen alias: `frozen_main.synthetic.internal_auth`. Member: `src/trusted_router/synthetic/internal_auth.py`.

SHA-256: `4aff6a78a81d3dbf2ae5156fd32882028985735bf90de270b4caa9c110dde9ba`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.leaderboard`

Frozen alias: `frozen_main.synthetic.leaderboard`. Member: `src/trusted_router/synthetic/leaderboard.py`.

SHA-256: `bbb5e0eb270441623113111b06b8648391993434599e72ccc89dc29c57936dac`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ProviderModelStats` | 72 |
| `ProviderStats` | 203 |
| `__create_fn__` | 1 |

## `trusted_router.synthetic.probes`

Frozen alias: `frozen_main.synthetic.probes`. Member: `src/trusted_router/synthetic/probes.py`.

SHA-256: `6c617c9a89395fb194a59e29ea902b5905f75701489a8ec9bd9895c648f3c732`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SyntheticTarget` | 76 |
| `_StreamObservation` | 2043 |
| `_StreamUsage` | 2036 |
| `__create_fn__` | 1 |

## `trusted_router.synthetic.remediator`

Frozen alias: `frozen_main.synthetic.remediator`. Member: `src/trusted_router/synthetic/remediator.py`.

SHA-256: `4443a5fc0205436f276630a4a46858cd2e8a6ca8044492c919a7fe81ed584601`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `Decision` | 84 |
| `__create_fn__` | 1 |

## `trusted_router.synthetic.rollups`

Frozen alias: `frozen_main.synthetic.rollups`. Member: `src/trusted_router/synthetic/rollups.py`.

SHA-256: `af7c5781d2727010e5754f53babf6bb0f391e58b398eba2363222852c7b93af1`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.route_health`

Frozen alias: `frozen_main.synthetic.route_health`. Member: `src/trusted_router/synthetic/route_health.py`.

SHA-256: `26a71e330306caa3ecfe43f0fd93ebe6ce24641beaf5352421a0600bd9396242`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `RouteHealthFlag` | 50 |
| `__create_fn__` | 1 |

## `trusted_router.synthetic.status`

Frozen alias: `frozen_main.synthetic.status`. Member: `src/trusted_router/synthetic/status.py`.

SHA-256: `77d45c9a6b109697d80ee13823617ac38cd71baf1d2b992ee9a0568cb1ffb28e`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.throughput`

Frozen alias: `frozen_main.synthetic.throughput`. Member: `src/trusted_router/synthetic/throughput.py`.

SHA-256: `878724a5ef467544b976ee77a2e357c1c8f1464f2cf3c7ecedcfea3e975fa7a9`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.synthetic.video_leaderboard`

Frozen alias: `frozen_main.synthetic.video_leaderboard`. Member: `src/trusted_router/synthetic/video_leaderboard.py`.

SHA-256: `0f558069a93e98fefa0ceb0694b383d824382b8c917334c78bac3d2d3d1db6c5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.token_exchange`

Frozen alias: `frozen_main.token_exchange`. Member: `src/trusted_router/token_exchange.py`.

SHA-256: `7143b8140f38f927002dfaf139176085f7181fb54f712f0d00381351f3cae35f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.trust`

Frozen alias: `frozen_main.trust`. Member: `src/trusted_router/trust.py`.

SHA-256: `64acdff283e577be484416adc66d969ea09e26b1cc4844d993a3e2dc9cebfbfc`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.trust_ownership`

Frozen alias: `frozen_main.trust_ownership`. Member: `src/trusted_router/trust_ownership.py`.

SHA-256: `7ab9b356269793e9688e139c66c09f8bc78afab02a2a1255dcd804b17ed3ee45`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `OwnerTrustMutationBudgetExceeded` | 21 |
| `WorkspaceOwnerLimitExceeded` | 17 |

## `trusted_router.trust_reconciliation`

Frozen alias: `frozen_main.trust_reconciliation`. Member: `src/trusted_router/trust_reconciliation.py`.

SHA-256: `920f5f457a62a749e4b81bbb8666a29adb3c92cf56a4e6cc8358785c4c346710`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `BackfillMarker` | 32 |
| `CanonicalTrustRecord` | 90 |
| `MarkerRequirement` | 62 |
| `OutstandingAdverse` | 242 |
| `ReconciliationDiff` | 122 |
| `__create_fn__` | 1 |

## `trusted_router.trust_tiers`

Frozen alias: `frozen_main.trust_tiers`. Member: `src/trusted_router/trust_tiers.py`.

SHA-256: `15e35838f39ebc7db3bfae142d8deb460217664f9c572ecb2b2be3c699e251ce`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `TrustTierDecision` | 180 |
| `__create_fn__` | 1 |

## `trusted_router.typed_balance`

Frozen alias: `frozen_main.typed_balance`. Member: `src/trusted_router/typed_balance.py`.

SHA-256: `e1c50229c71747041b64c31d22866f0e3af9fef515f04d3c945eb7c1e04bbbf5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `LiveCreditSummary` | 16 |
| `RemainingCreditSummary` | 23 |

## `trusted_router.types`

Frozen alias: `frozen_main.types`. Member: `src/trusted_router/types.py`.

SHA-256: `bb6bad99ee8193390b43cece44f311e0a15a7e94ec97ad57681e48fd37b90df0`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `ErrorType` | 61 |
| `IdentityVerificationStatus` | 39 |
| `UsageType` | 12 |
| `UsageType.coerce` | 26 |
| `UsageType.for_endpoint` | 22 |

## `trusted_router.user_model_rules`

Frozen alias: `frozen_main.user_model_rules`. Member: `src/trusted_router/user_model_rules.py`.

SHA-256: `afd0bc3e37723312f972ecf1cd09d05a1ecd933158f1b9a38d817002ed8952ec`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `DispatchBudget` | 56 |
| `__create_fn__` | 1 |
| `__create_fn__.<locals>.__init__` | 2 |

## `trusted_router.veriff_verify`

Frozen alias: `frozen_main.veriff_verify`. Member: `src/trusted_router/veriff_verify.py`.

SHA-256: `5a3b30eca0f855e1bf0f327d9dc948ab506d550da0c86da7f87d1d651a3004c2`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `VeriffVerificationError` | 7 |

## `trusted_router.verification`

Frozen alias: `frozen_main.verification`. Member: `src/trusted_router/verification.py`.

SHA-256: `e678b4fe7d7fd53894123d956c71e697f584edee81dc5c8b28262ff449beaf50`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.verification_gates`

Frozen alias: `frozen_main.verification_gates`. Member: `src/trusted_router/verification_gates.py`.

SHA-256: `90b6294fbcc9efc9d84eb450e22f76f22b4a334cdccc12158fc09372d970a68f`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.video_billing`

Frozen alias: `frozen_main.video_billing`. Member: `src/trusted_router/video_billing.py`.

SHA-256: `a2c7309f39e7f6b575bd3a781ce54b47b6e58f26a4cbec9f896afda51fd92e1a`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.views`

Frozen alias: `frozen_main.views`. Member: `src/trusted_router/views.py`.

SHA-256: `e009bcd0c0a70ca242cea41df4e34b0848588f6536e869388469f45e2e9d61b5`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |

## `trusted_router.wafer_policy`

Frozen alias: `frozen_main.wafer_policy`. Member: `src/trusted_router/wafer_policy.py`.

SHA-256: `41c995561e490e2bfac9bd3d47adf84723c665e726f3ff5b1443dbf6e412e9d6`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `_wafer_zdr_index` | 15 |
| `wafer_zdr_support` | 40 |

## `trusted_router.wallet_auth`

Frozen alias: `frozen_main.wallet_auth`. Member: `src/trusted_router/wallet_auth.py`.

SHA-256: `ed32ea418f6bf66b7aa96b05995a705b6e24351d0663e22f15608b9efb102026`.

| Callable / executed code unit | Code line |
|---|---|
| `<module>` | 1 |
| `SiweMessage` | 20 |
| `__create_fn__` | 1 |
