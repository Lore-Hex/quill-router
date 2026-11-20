Features and unit prices vary with model services. This topic introduces the billing formulas and unit prices for each model to help you check and compare model prices.

> **Tip**

> To learn about the billing method and detailed billing rules, see [Model service billing](https://ai.byteplus.com/ark/region:ap-southeast-1/docs/model-service-pricing).

> **Pricing notes**

* > Unless otherwise specified, prices use the following units:

   * > Token prices are **USD per million tokens**. Cache\-storage prices are **USD per million tokens per hour**.

   * > Image generation prices are **per image**.

   * > 3D generation prices are **per call**.

* > The peak and off\-peak hours for DeepSeek V4.1 Flash series models are as follows:

   * > **Peak hours**: (UTC+8) Monday to Friday, 09:00\-12:00 and 14:00\-18:00.

   * > **Off\-peak hours**: All times other than the peak hours listed above are off\-peak hours.

## Large language models

### Pricing

#### Non\-audio input

**Standard**

|Provider |Model ID |Prompt length (K tokens) |Input |Cached input |Cache storage |Output |
|---|---|---|---|---|---|---|
|ByteDance |`dola-seed-2-1-turbo` |[0, 256] |$0.5 |$0.1 |$0.0083 |$2.5 |
||`seed-2-0-lite-260428` |[0, 128] |$0.25 |$0.05 |$0.0083 |$2.00 |
|||(128, 256] |$0.50 |$0.10 |$0.0083 |$4.00 |
||`seed-2-0-mini-260428` |[0, 128] |$0.10 |$0.02 |$0.0083 |$0.40 |
|||(128, 256] |$0.20 |$0.04 |$0.0083 |$0.80 |
||`seed-2-0-pro-260328` |[0, 128] |$0.50 |$0.10 |$0.0083 |$3.00 |
|||(128, 256] |$1.00 |$0.20 |$0.0083 |$6.00 |
||`seed-2-0-code-preview-260328` |[0, 128] |$0.50 |$0.10 |$0.0083 |$3.00 |
|||(128, 256] |$1.00 |$0.20 |$0.0083 |$6.00 |
|DeepSeek |`deepseek-v4-1-flash-economy-261017` |Off\-peak hours |$0.12 |$0.002 |$0.0083 |$0.48 |
|||Peak hours |$0.24 |$0.005 |$0.0083 |$0.96 |
||`deepseek-v4-1-flash-261017` |Off\-peak hours |$0.15 |$0.003 |$0.0083 |$0.60 |
|||Peak hours |$0.30 |$0.006 |$0.0083 |$1.20 |
||`deepseek-v4-1-flash-260910` |Off\-peak hours |$0.15 |$0.003 |$0.0083 |$0.60 |
|||Peak hours |$0.30 |$0.006 |$0.0083 |$1.20 |
||`deepseek-v4-pro-ga-260813` |Not applicable |$1.32 |$0.044 |$0.0083 |$3.96 |
||`deepseek-v4-flash-ga-260731` |Not applicable |$0.44 |$0.014 |$0.0083 |$1.32 |
||`deepseek-v4-pro-260425` |Not applicable |$1.32 |$0.044 |$0.0083 |$3.96 |
||`deepseek-v4-flash-260425` |Not applicable |$0.44 |$0.014 |$0.0083 |$1.32 |
|Z.ai |`glm-5-3-flash-260828` |Not applicable |$0.15 |$0.03 |$0.0083 |$0.5 |
||`glm-5-2-260617` |Not applicable |$1.4 |$0.26 |$0.0083 |$4.4 |

**Fast**

|Model ID |Prompt length (K tokens) |Input |Cached input |Output |
|---|---|---|---|---|
|`seed-2-0-lite-260428` |[0, 128] |$0.50 |$0.10 |$4.00 |
||(128, 256] |$1.00 |$0.20 |$8.00 |

**Flex**

|Provider |Model ID |Prompt length (K tokens) |Input |Cached input |Output |
|---|---|---|---|---|---|
|ByteDance |`dola-seed-2-1-turbo` |[0, 256] |$0.25 |$0.10 |$1.25 |
||`seed-2-0-pro-260328` |[0, 128] |$0.25 |$0.10 |$1.50 |
|||(128, 256] |$0.50 |$0.20 |$3.00 |
||`seed-2-0-lite-260428` |[0, 128] |$0.125 |$0.05 |$1.00 |
|||(128, 256] |$0.25 |$0.10 |$2.00 |
||`seed-2-0-mini-260428` |[0, 128] |$0.05 |$0.02 |$0.20 |
|||(128, 256] |$0.10 |$0.04 |$0.40 |
|DeepSeek |`deepseek-v4-pro-ga-260813` |Not applicable |$0.66 |$0.044 |$1.98 |
||`deepseek-v4-1-flash-260910` |Off\-peak hours |$0.15 |$0.003 |$0.60 |
||`deepseek-v4-flash-ga-260731` |Not applicable |$0.22 |$0.014 |$0.66 |

**Batch**

|Provider |Model ID |Prompt length (K tokens) |Input |Cached input |Output |
|---|---|---|---|---|---|
|ByteDance |`seed-2-0-lite-260428` |[0, 128] |$0.125 |$0.05 |$1.00 |
|||(128, 256] |$0.25 |$0.10 |$2.00 |
||`seed-2-0-mini-260428` |[0, 128] |$0.05 |$0.02 |$0.20 |
|||(128, 256] |$0.10 |$0.04 |$0.40 |
|DeepSeek |`deepseek-v4-pro-ga-260813` |Not applicable |$0.66 |$0.044 |$1.98 |
||`deepseek-v4-1-flash-economy-261017` |Off\-peak hours |$0.12 |$0.002 |$0.48 |
||`deepseek-v4-1-flash-260910` |Off\-peak hours |$0.15 |$0.003 |$0.60 |
||`deepseek-v4-flash-260425` |Not applicable |$0.22 |$0.014 |$0.66 |
||`deepseek-v4-flash-ga-260731` |Not applicable |$0.22 |$0.014 |$0.66 |
||`deepseek-v4-pro-260425` |Not applicable |$0.66 |$0.044 |$1.98 |

#### Audio input

Audio input prices are listed by inference tier below. Output token prices are listed in the tables above.

**Standard**

|Model ID |Prompt length (K tokens) |Input |Cached input |
|---|---|---|---|
|`seed-2-0-lite-260428` |[0, 128] |$3.75 |$0.75 |
||(128, 256] |$7.50 |$1.50 |
|`seed-2-0-mini-260428` |[0, 128] |$1.50 |$0.30 |
||(128, 256] |$3.00 |$0.60 |

**Fast**

|Model ID |Prompt length (K tokens) |Input |Cached input |
|---|---|---|---|
|`seed-2-0-lite-260428` |[0, 128] |$7.50 |$1.50 |
||(128, 256] |$15.00 |$3.00 |

**Flex**

|Model ID |Prompt length (K tokens) |Input |Cached input |
|---|---|---|---|
|`seed-2-0-lite-260428` |[0, 128] |$1.875 |$0.75 |
||(128, 256] |$3.75 |$1.50 |
|`seed-2-0-mini-260428` |[0, 128] |$0.75 |$0.30 |
||(128, 256] |$1.50 |$0.60 |

### Billing rules

* **Standard:**  `Cost = Input unit price × input tokens + Cached input unit price × cached input tokens + Cache-storage unit price × cache-storage tokens × duration + Output unit price × output tokens`

* **Flex:**  Input and output prices are approximately 50% of Standard prices. Cached input prices are the same as Standard prices. Flex supports implicit caching only, with no cache\-storage fee.

* **Batch:**  `Cost = Input unit price × input tokens + Cached input unit price × cached input tokens + Output unit price × output tokens`. Some models support transparent prefix caching without additional configuration.

* **Tiered billing:**  For models with prompt\-length tiers, prices are determined by the prompt length of each request.

### Billing examples

A request to `seed-2-0-lite-260428` with 200K input tokens and 14K output tokens uses the (128, 256]K pricing tier.

## Video generation models

### Pricing

> **Tip**

> Video pricing is determined by both the **token rate** and **token consumption**. Under otherwise identical conditions:

* > A video generated at a higher resolution costs more than one generated at a lower resolution.

* > A video generated with video input costs more than one generated without video input.

> You can use the [Seedance 2.0 Series Pricing Calculator](https://bytedance.larkoffice.com/share/base/form/shrcnIHSU1yg0GtzCX4pyW3g3yb) and [Seedance 2.5 Series Pricing Calculator](https://bytedance.larkoffice.com/share/base/form/shrcnIYBa28VajWRTpAPEwR4icf) to estimate the cost of each video.

**Input without video**

|Model |Online inference |||Offline inference |
|---|---|---|---|---|
| |480p and 720p |1080p |4K | |
|dreamina\-seedance\-2\-5\-flash\-261115 |$7.0 |Not supported |Not supported |Not supported |
|dreamina\-seedance\-2\-5\-260628 |$10.70 |$11.7 |$4.7 |Not supported |
|dreamina\-seedance\-2\-0\-260128 |$7.0 |$7.7 |$4.0 |Not supported |
|dreamina\-seedance\-2\-0\-fast\-260128 |$5.6 |Not supported |Not supported |Not supported |
|dreamina\-seedance\-2\-0\-mini\-260615 |$3.5 |Not supported |Not supported |Not supported |
|seedance\-1\-0\-pro\-250528 |$2.5 |||$1.25 |
|seedance\-1\-0\-pro\-fast\-251015 |$1 |||$0.5 |

**Input with video**

|Model |Online inference |||Offline inference |
|---|---|---|---|---|
| |480p and 720p |1080p |4K | |
|dreamina\-seedance\-2\-5\-flash\-261115 |$4.3 |Not supported |Not supported |Not supported |
|dreamina\-seedance\-2\-5\-260628 |$6.40 |$7.0 |$2.9 |Not supported |
|dreamina\-seedance\-2\-0\-260128 |$4.3 |$4.7 |$2.4 |Not supported |
|dreamina\-seedance\-2\-0\-fast\-260128 |$3.3 |Not supported |Not supported |Not supported |
|dreamina\-seedance\-2\-0\-mini\-260615 |$2.1 |Not supported |Not supported |Not supported |

### Billing rules
## Image generation models
