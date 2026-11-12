# Managed Azure Foundry ZDR

Reviewed November 12, 2026. Microsoft's approval (application 3705421) covers
the existing managed subscription used by the Azure provider, not the new
enterprise subscription. The approval email is retained privately; subscription
IDs and correspondence are not published here.

Read-only Azure management checks confirmed `ContentLogging=false` on
`trustedrouter-foundry-eastus2` in `tr-model-providers`. This is the resource
used by the enclave's fixed Azure OpenAI endpoint and the account sync in
`scripts/providers/sync_azure_foundry.py`.

## Qualified routes

The ten reviewed catalog deployments use the verified resource: Command A,
Microsoft Decision-1, Kimi K2.5/K2.6/K2.7 Code, GPT-5-mini, and both reasoning
and non-reasoning variants of Grok 4.1 Fast and Grok 4.20. Azure management
reports each deployment as succeeded. The catalog sync pins model versions
with `NoAutoUpgrade`.

Microsoft's current policy applies to Models sold by Azure, including these
Azure-hosted third-party models, not only OpenAI. The account contact's
November 11 reply mentions all Foundry models in the enterprise subscription;
that access statement alone is not the retention evidence. The privacy policy,
approved resource setting and exact deployed routes establish the scope here.

`managed_retention.REVIEWED_ROUTES` is the reviewed allowlist, independent of
automatic discovery. Unknown pairs, partner-hosted services, Speech previews
and BYOK remain excluded from strict ZDR. A change of subscription,
resource, model version or retention features requires another review before
retaining this claim. This approval does not create confidential-compute or
E2EE evidence for Azure inference.

## Scope and recheck

Microsoft's modified monitoring removes abuse-monitoring content storage and
human review; real-time automated safety processing may continue. Stateful
services (including stored completions, Assistants, files and persisted
Responses history) are not covered by this route's claim.

Recheck the resource without reading keys or sending customer content:

```sh
az cognitiveservices account show \
  --resource-group tr-model-providers \
  --name trustedrouter-foundry-eastus2 \
  --query 'capabilities[?name==`ContentLogging`]'
```

Require the value `false`; missing is not equivalent to disabled. Recheck when
moving credentials/resources or reviewing new models. The new enterprise
subscription needs its own written approval and configuration verification.

Source: [Microsoft data privacy and ContentLogging verification](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy).

Model scope: [Models sold and hosted by Azure](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure).
