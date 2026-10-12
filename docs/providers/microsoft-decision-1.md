# Microsoft Decision-1 on Azure

Verified 2026-10-12. Preview, version 1. Canonical model ID:
`microsoft/microsoft-decision-1`.

## Contract

- API: `POST https://api.trustedrouter.com/api/alpha/decide`.
  The compatibility alias `/v1/decide` accepts the same contract.
- Input: text or JSON `state`, and keyed `questions`.
- Question types: `boolean` (also `noul`), `choice`, `score`.
- Context: 32,768 tokens, per Microsoft's model card.
- No chat generation, streaming, tools, images or reasoning controls.
- Direct provider: Azure, deployment `tr-decision-1`, GlobalStandard, version 1,
  `NoAutoUpgrade`. Account: `trustedrouter-foundry-eastus2`.
- The enclave sends `api-key` to the fixed account-scoped
  `/providers/microsoft/v1/systemone` endpoint and refuses redirects.
- Azure reports `model: microsoft-decision-1`, input tokens and output tokens.
  It does not echo the deployment name. Outputs are counted but free.
- Public model metadata makes no additional upstream ZDR or confidential-compute claim.

```json
{
  "model": "microsoft/microsoft-decision-1",
  "state": "I was charged twice and need a refund.",
  "questions": {
    "refund": {"type": "boolean", "instructions": "Is a refund requested?"},
    "team": {
      "type": "choice",
      "instructions": "Which team should handle this ticket?",
      "criteria": {"billing": "Charges and refunds", "engineering": "Software bugs"}
    }
  }
}
```

## Evidence and Refresh

Direct paid smoke returned HTTP 200 for all three decision types, with 93 input
tokens and 3 output tokens. Refund probability was above 0.99 and the selected
team was billing. This is protocol verification, not an accuracy benchmark.

Microsoft's Retail Prices API returned product `Microsoft Decision Models`,
SKU `Microsoft Decision 1`, meter `Microsoft Decision 1 1M Tokens`, unit `1M`,
price $0.042 USD, effective 2026-10-01. Standard TR price including the 5.5% fee
is $0.04431 per million input tokens, with zero output charge. Refresh selects
this exact input meter and fails closed if a paid output meter appears.

The existing Azure account sync now admits this decision capability independently
of chat, tool and vision tests. Its canary verifies yes/no, category and score
responses and usage. The daily price refresh preserves decision metadata.

The account ARM model catalog currently reports inference retirement at
2027-02-04 00:00 UTC. The route retires at that timestamp; a successor requires
review rather than an automatic checkpoint substitution.

Sources:

- [Model card](https://ai.azure.com/catalog/models/Microsoft-Decision-1)
- [Protocol](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
- [Launch and pricing](https://commandline.microsoft.com/microsoft-decision-1-model-foundry/)
- [Retail feed](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Foundry%20Models%27%20and%20armRegionName%20eq%20%27eastus2%27)

## Release Order

Deploy the enclave Azure decision adapter before publishing the catalog route.
Then verify the public model entry and a small authenticated `/api/alpha/decide` request.
Provider failures remain provider-health failures, separate from router availability.

Initial availability is the main GCP-backed API. The native Azure decision
adapter is QCP release `bfac8577`; AWS and Azure gateway fleets must receive
and verify that adapter before their standalone control planes offer this model.
`catalog_deployment.py` removes it from discovery and routing on those planes
(and unidentified production regions), even when automatic control-plane
reconciliation advances them. Other models and cloud release gates are unchanged.
Lift this scoped hold only after each remaining gateway fleet passes an
authenticated decision smoke. GCP control-plane regions, including the billing
failover region, share the upgraded GCP gateway fleet.
