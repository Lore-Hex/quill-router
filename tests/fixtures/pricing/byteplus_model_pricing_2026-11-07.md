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

Only the following models support audio input for Standard and Flex inference. Output token prices are listed in the tables above.

**Standard**

|Model ID |Prompt length (K tokens) |Input |Cached input |
|---|---|---|---|
|`seed-2-0-lite-260428` |[0, 128] |$3.75 |$0.75 |
||(128, 256] |$7.50 |$1.50 |
|`seed-2-0-mini-260428` |[0, 128] |$1.50 |$0.30 |
||(128, 256] |$3.00 |$0.60 |

**Flex**

|Model ID |Prompt length (K tokens) |Input |Cached input |
|---|---|---|---|
|`seed-2-0-lite-260428` |[0, 128] |$1.875 |$0.75 |
||(128, 256] |$3.75 |$1.50 |
|`seed-2-0-mini-260428` |[0, 128] |$0.75 |$0.30 |
||(128, 256] |$1.50 |$0.60 |

Retiring models

These models are retiring and will be retired on November 11, 2026.

**Standard**

|Model ID |Recommended replacement |Prompt length (K tokens) |Input |Cached input |Cache storage |Output |
|---|---|---|---|---|---|---|
|`seed-2-0-lite-260228` |`dola-seed-2-0-lite-260428` |[0, 128] |$0.25 |$0.05 |$0.0083 |$2.00 |
|||(128, 256] |$0.50 |$0.10 |$0.0083 |$4.00 |
|`seed-2-0-mini-260215` |`dola-seed-2-0-mini-260428` |[0, 128] |$0.10 |$0.02 |$0.0083 |$0.40 |
|||(128, 256] |$0.20 |$0.04 |$0.0083 |$0.80 |
|`seed-1-8-251228` |`dola-seed-2-0-lite-260428` |[0, 128] |$0.25 |$0.05 |$0.0083 |$2.00 |
|||(128, 256] |$0.50 |$0.05 |$0.0083 |$4.00 |
|`seed-1-6-250915` |`dola-seed-2-0-lite-260428` |[0, 128] |$0.25 |$0.05 |$0.0083 |$2.00 |
|||(128, 256] |$0.50 |$0.05 |$0.0083 |$4.00 |
|`seed-1-6-flash-250715` |`dola-seed-2-0-mini-260428` |[0, 128] |$0.075 |$0.015 |$0.0083 |$0.30 |
|||(128, 256] |$0.10 |$0.015 |$0.0083 |$0.80 |
|`deepseek-v3-2-251201` |`deepseek-v4-flash-ga-260731` |[0, 32] |$0.28 |$0.056 |$0.0083 |$0.42 |
|||(32, 128] |$0.56 |$0.056 |$0.0083 |$0.84 |
|`glm-4-7-251222` |`dola-seed-2-1-turbo-260628` |Not applicable |$0.6 |$0.11 |$0.0083 |$2.2 |

**Flex**

|Availability |
|---|
|No retiring model pricing is listed for Flex inference. |

**Batch**

|Model ID |Recommended replacement |Prompt length (K tokens) |Input |Cached input |Output |
|---|---|---|---|---|---|
|`seed-2-0-lite-260228` |`dola-seed-2-0-lite-260428` |[0, 128] |$0.125 |$0.05 |$1.00 |
|||(128, 256] |$0.25 |$0.10 |$2.00 |
|`seed-2-0-mini-260215` |`dola-seed-2-0-mini-260428` |[0, 128] |$0.05 |$0.02 |$0.20 |
|||(128, 256] |$0.10 |$0.04 |$0.40 |
|`seed-1-8-251228` |`dola-seed-2-0-lite-260428` |[0, 128] |$0.125 |$0.05 |$1.00 |
|||(128, 256] |$0.25 |$0.05 |$2.00 |
|`seed-1-6-250915` |`dola-seed-2-0-lite-260428` |[0, 128] |$0.125 |$0.05 |$1.00 |
|||(128, 256] |$0.25 |$0.05 |$2.00 |
|`seed-1-6-flash-250715` |`dola-seed-2-0-mini-260428` |[0, 128] |$0.038 |$0.015 |$0.15 |
|||(128, 256] |$0.05 |$0.015 |$0.40 |
|`deepseek-v3-2-251201` |`deepseek-v4-flash-ga-260731` |[0, 32] |$0.14 |$0.056 |$0.21 |
|||(32, 128] |$0.28 |$0.056 |$0.42 |
|`glm-4-7-251222` |`dola-seed-2-1-turbo-260628` |Not applicable |$0.3 |$0.11 |$1.1 |

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
|dreamina\-seedance\-2\-5\-260628 |$10.70 |$11.7 |Not supported |Not supported |
|dreamina\-seedance\-2\-0\-260128 |$7.0 |$7.7 |$4.0 |Not supported |
|dreamina\-seedance\-2\-0\-fast\-260128 |$5.6 |Not supported |Not supported |Not supported |
|dreamina\-seedance\-2\-0\-mini\-260615 |$3.5 |Not supported |Not supported |Not supported |
|seedance\-1\-0\-pro\-250528 |$2.5 |||$1.25 |
|seedance\-1\-0\-pro\-fast\-251015 |$1 |||$0.5 |

**Input with video**

|Model |Online inference |||Offline inference |
|---|---|---|---|---|
| |480p and 720p |1080p |4K | |
|dreamina\-seedance\-2\-5\-260628 |$6.40 |$7.0 |Not supported |Not supported |
|dreamina\-seedance\-2\-0\-260128 |$4.3 |$4.7 |$2.4 |Not supported |
|dreamina\-seedance\-2\-0\-fast\-260128 |$3.3 |Not supported |Not supported |Not supported |
|dreamina\-seedance\-2\-0\-mini\-260615 |$2.1 |Not supported |Not supported |Not supported |

Retiring models

This model is retiring and will be retired on November 11, 2026.

|Model |Online inference ||Offline inference ||
|---|---|---|---|---|
| |Video with audio |Video without audio |Video with audio |Video without audio |
|seedance\-1\-5\-pro\-251215 |$2.4 |$1.2 |$1.2 |$0.6 |

### Billing rules

* You are only charged for successfully generated videos. No fee is charged if generation fails due to reasons such as content moderation.

* **Estimated video price:**  `Token unit price × token consumption`

* **Estimated token consumption:**  `(Input video duration + output video duration) × output video width × output video height × output video frame rate / 1024`

* Some models support Draft mode:

   * Dreamina Seedance 2.5 generates Draft videos at a fixed 480p resolution. The token consumption and token rate are the same as normal 480p videos.

   * Seedance 1.5 Pro uses the following formula: `Draft video token consumption = Estimated normal video token consumption × conversion factor`. The conversion factor is 0.7 for silent videos and 0.6 for videos with audio.

* Token consumption values are estimates. Actual consumption is determined by `usage.completion_tokens` in the API response.

> **Minimum token consumption**

> For the Dreamina Seedance 2.0 series and Dreamina Seedance 2.5, minimum token consumption applies when the input includes video:

* > If estimated token consumption is below the minimum, the video is billed at the minimum token consumption.

* > The minimum depends on resolution, aspect ratio, and output duration. Check the [Dreamina Seedance 2.0 series minimum token consumption table](https://bytedance.larkoffice.com/wiki/H0fUwHPxtiHayOk6CVpcZzKqnHJ?table=tblnV1KjhYJO12Zh&view=vewWPcN5zJ) or [Dreamina Seedance 2.5 minimum token consumption table](https://bytedance.larkoffice.com/wiki/H0fUwHPxtiHayOk6CVpcZzKqnHJ?table=tblTUnmKaxBSqpU7&view=vewpCgsiHf).

### Billing examples

The following estimated prices illustrate typical input and output specifications.

#### Dreamina Seedance 2.5

**Input without video**

|Resolution |Aspect ratio |Output video duration (seconds) |Price (per video) |
|---|---|---|---|
|480p |16:9 |5 |* $0.514 per video<br><br>* $0.103 per second |
|720p |16:9 |5 |* $1.156 per video<br><br>* $0.231 per second |
|1080p |16:9 |5 |* $2.843 per video<br><br>* $0.569 per second |

**Input with video**

|Resolution |Aspect ratio |Input duration (seconds) |Output duration (seconds) |Price (per video) |
|---|---|---|---|---|
|480p |16:9 |2\-30 |5 |$0.553\-$2.152<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 30 seconds input |
|720p |16:9 |2\-30 |5 |$1.244\-$4.838<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 30 seconds input |
|1080p |16:9 |2\-30 |5 |$3.062\-$11.907<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 30 seconds input |

#### Dreamina Seedance 2.0 series

**Input without video**

|Resolution |480p |720p |1080p |4K |
|---|---|---|---|---|
|Aspect ratio |16:9 |16:9 |16:9 |16:9 |
|Output video duration (seconds) |5 |5 |5 |5 |
|Dreamina Seedance 2.0 |* $0.35 per video<br><br>* $0.07 per second |* $0.76 per video<br><br>* $0.15 per second |* $1.87 per video<br><br>* $0.37 per second |* $3.89 per video<br><br>* $0.78 per second |
|Dreamina Seedance 2.0 Fast |* $0.28 per video<br><br>* $0.06 per second |* $0.60 per video<br><br>* $0.12 per second |Not supported |Not supported |
|Dreamina Seedance 2.0 Mini |* $0.18 per video<br><br>* $0.04 per second |* $0.38 per video<br><br>* $0.08 per second |Not supported |Not supported |

**Input with video**

|Resolution |480p |720p |1080p |4K |
|---|---|---|---|---|
|Aspect ratio |16:9 |16:9 |16:9 |16:9 |
|Input duration (seconds) |2\-15 |2\-15 |2\-15 |2\-15 |
|Output duration (seconds) |5 |5 |5 |5 |
|Dreamina Seedance 2.0 |$0.39\-$0.86<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |$0.84\-$1.86<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |$2.06\-$4.57<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |$4.20\-$9.33<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |
|Dreamina Seedance 2.0 Fast |$0.30\-$0.66<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |$0.64\-$1.43<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |Not supported |Not supported |
|Dreamina Seedance 2.0 Mini |$0.19\-$0.42<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |$0.41\-$0.91<br><br><br>* Lowest price: 2\-4 seconds input<br><br>* Highest price: 15 seconds input |Not supported |Not supported |

#### Seedance 1.0 Pro

To get more estimated prices, please use the [Price Calculator - SD 1.0 Pro](https://bytedance.larkoffice.com/share/base/form/shrcna8QRuYvU0vVl1YrRxfrkvg).

|Resolution |Ratio |Long side × short side (px) |Frame rate (frame/s) |Duration (seconds) |Usage (tokens) |Price |
|---|---|---|---|---|---|---|
|480p |16:9 |864×480 |24 |5 |48600 |$0.12 |
||16:9 |864×480 |24 |10 |97000 |$0.24 |
||4:3 |736×544 |24 |5 |46920 |$0.12 |
||4:3 |736×544 |24 |10 |93840 |$0.23 |
||1:1 |640×640 |24 |5 |48000 |$0.12 |
||1:1 |640×640 |24 |10 |96000 |$0.24 |
||21:9 |960×416 |24 |5 |46800 |$0.12 |
||21:9 |960×416 |24 |10 |93600 |$0.23 |
|720p |16:9 |1248×704 |24 |5 |102960 |$0.26 |
||16:9 |1248×704 |24 |10 |205920 |$0.51 |
||4:3 |1120×832 |24 |5 |109200 |$0.27 |
||4:3 |1120×832 |24 |10 |218400 |$0.55 |
||1:1 |960×960 |24 |5 |108000 |$0.27 |
||1:1 |960×960 |24 |10 |216000 |$0.54 |
||21:9 |1504×640 |24 |5 |112800 |$0.28 |
||21:9 |1504×640 |24 |10 |225600 |$0.56 |
|1080p |16:9 |1920×1088 |24 |5 |244800 |$0.61 |
||16:9 |1920×1088 |24 |10 |489600 |$1.22 |
||4:3 |1664×1248 |24 |5 |243360 |$0.61 |
||4:3 |1664×1248 |24 |10 |486720 |$1.22 |
||1:1 |1440×1440 |24 |5 |243000 |$0.61 |
||1:1 |1440×1440 |24 |10 |486000 |$1.22 |
||21:9 |2176×928 |24 |5 |236640 |$0.59 |
||21:9 |2176×928 |24 |10 |473280 |$1.18 |

#### Seedance 1.0 Pro Fast

To get more estimated prices, please use the [Price Calculator - SD 1.0 Pro](https://bytedance.larkoffice.com/share/base/form/shrcna8QRuYvU0vVl1YrRxfrkvg).

|Resolution |Ratio |Long side × short side (px) |Frame rate (frame/s) |Duration (seconds) |Usage (tokens) |Price |
|---|---|---|---|---|---|---|
|480p |16:9 |864×480 |24 |5 |48600 |$0.05 |
||16:9 |864×480 |24 |10 |97000 |$0.10 |
||4:3 |736×544 |24 |5 |46920 |$0.05 |
||4:3 |736×544 |24 |10 |93840 |$0.09 |
||1:1 |640×640 |24 |5 |48000 |$0.05 |
||1:1 |640×640 |24 |10 |96000 |$0.10 |
||21:9 |960×416 |24 |5 |46800 |$0.05 |
||21:9 |960×416 |24 |10 |93600 |$0.09 |
|720p |16:9 |1248×704 |24 |5 |102960 |$0.10 |
||16:9 |1248×704 |24 |10 |205920 |$0.21 |
||4:3 |1120×832 |24 |5 |109200 |$0.11 |
||4:3 |1120×832 |24 |10 |218400 |$0.22 |
||1:1 |960×960 |24 |5 |108000 |$0.11 |
||1:1 |960×960 |24 |10 |216000 |$0.22 |
||21:9 |1504×640 |24 |5 |112800 |$0.11 |
||21:9 |1504×640 |24 |10 |225600 |$0.23 |
|1080p |16:9 |1920×1088 |24 |5 |244800 |$0.24 |
||16:9 |1920×1088 |24 |10 |489600 |$0.49 |
||4:3 |1664×1248 |24 |5 |243360 |$0.24 |
||4:3 |1664×1248 |24 |10 |486720 |$0.49 |
||1:1 |1440×1440 |24 |5 |243000 |$0.24 |
||1:1 |1440×1440 |24 |10 |486000 |$0.49 |
||21:9 |2176×928 |24 |5 |236640 |$0.24 |
||21:9 |2176×928 |24 |10 |473280 |$0.47 |

## Image generation models

### Pricing

|Model ID |Input image |Output image |
|---|---|---|
|`dola-seedream-5-0-pro-260628` |* First image: Free<br><br>* From the second image: $0.003<br><br>> Pricing varies by image generation scenario. |* Single\-image generation:<br><br>   * Up to 2.61 million pixels (1.5K or lower): $0.045<br><br>   * More than 2.61 million pixels (higher than 1.5K): $0.09<br><br>* Layer decomposition:<br><br>   * Up to 2.61 million pixels (1.5K or lower): $0.0225<br><br>   * More than 2.61 million pixels (higher than 1.5K): $0.045 |
|`dola-seedream-5-0-flash-260915` |Free |$0.018 |
|`seedream-5-0-lite-260128` |Free |$0.035 |
|`seedream-4-5-251128` |Free |$0.04 |
|`seedream-4-0-250828` |Free |$0.03 |
|`seededit-3-0-i2i-250628` |Free |$0.03 |

### Billing rules

* Images that are not successfully output due to reasons such as content moderation are not billed.

* In Seedream 5.0 pro layer decomposition, layers returned by the same request may fall into different pixel tiers. Each layer is billed separately based on its actual pixel tier.

* For Seedream 5.0 lite, Seedream 4.5, and Seedream 4.0, sequential image generation is billed based on the actual number of generated images.

## 3D generation models

### Pricing

|Provider |Model ID |Generation mode |Price |Calculation formula |
|---|---|---|---|---|
|Deemos |`hyper3d-gen2-260112` |* White model<br><br>* Textured model<br><br>* PBR material model<br><br>* Textured PBR material model |$0.399 |30K tokens per call × $0.0133 per K tokens |
|Hitem3D |`hitem3d-2-0-251223` |Standard white model |$0.8 |40K tokens per call × $0.020 per K tokens |
|||Standard textured model |$1.4 |70K tokens per call × $0.020 per K tokens |
|||High\-precision white model |$1.2 |60K tokens per call × $0.020 per K tokens |
|||High\-precision textured model |$1.8 |90K tokens per call × $0.020 per K tokens |

### Billing rules

Billing is based on the number of successfully output 3D files.

## Embedding models

### Pricing

Input, including images, is converted into tokens for billing.

|Model |Service type |Unit price |Free quota |
|`skylark-embedding-vision`<br><br>^^ |Image input<br><br>Text input |$0.325<br><br>$0.125 |500K tokens |
|---|---|---|---|

### Billing rules

* **Billing formula:**  `Inference cost = Text input unit price × text tokens + Image input unit price × image tokens`

* **Image tokens:**  `(width in px × height in px) / 784`, up to 1,312 tokens per image.

## Model unit

### Pricing

You can flexibly combine **Postpaid (hourly)**  and **Prepaid (monthly)**  billing options.

The platform also provides a [Model Unit Calculator](https://ai.byteplus.com/ark/region:ap-southeast-1/endpoint/create) (available on the order page after login) to help you estimate the required machine types and quantities. It is strongly recommended to perform stress testing with real business traffic to accurately determine the appropriate machine specifications and capacity.

For more information about how to choose a plan, see [What are the differences between model units of different machine types? How to choose a machine type?](https://ai.byteplus.com/ark/region:ap-southeast-1/docs/modelark-faq#112a7e97)

> **Tip**

> This is a Beta capability only open to invited users. To use it, submit [a test application ticket](https://console.byteplus.com/workorder/create?step=2&SubProductID=P00001514).

|Machine type |Billing method |Price per unit |
|---|---|---|
|Flavor\-A |Postpaid (hourly)<br><br>Prepaid (monthly) |$4.20<br><br>$2,800.00 |
|Flavor\-C |Postpaid (hourly)<br><br>Prepaid (monthly) |$1.80<br><br>$1,300.00 |

### Billing rules

Model units are billed based on the selected machine type and usage duration, and pricing is independent of the model itself.

## Managed Agents

### Pricing

Billing is calculated as the sum of three items: Tokens actually consumed during agent operation, agent runtime duration, and number of tool calls.

|Billing item |Meaning |Billing rules |Unit price |
|---|---|---|---|
|Model invocation |Fees incurred by the agent invoking the model. |Postpaid based on token usage. The unit price is subject to the actually invoked model. Only Standard inference is supported. Bills are generated hourly. |See the **Standard** tab under [Large language models](https://ai.byteplus.com/ark/region:ap-southeast-1/docs/model-pricing#large-language-models) |
|Agent runtime |The cloud\-isolated sandbox of the agent. It is used to execute code, read and write files, and run command line tools within a session, and is automatically reclaimed after the session ends. |Billing only applies when the session status is `running`. No billing applies when the status is `idle` (waiting for the user's next message or tool confirmation), `rescheduling`, or `terminated`. Runtime is accurate to the second, and bills are generated hourly. |$0.08/hour |
|Tool calling |Fees incurred by the agent invoking tools. Currently supported tools include `web_search` (real\-time internet search, returns web page titles, abstracts, and links) and `web_fetch` (web content crawling, returns body Markdown or refined text). |Billed based on the actual number of invocations by the agent, with bills generated hourly. |* `web_search`: $0.006/request<br><br>* `web_fetch`: Free for a limited time |

### Billing rules

Billing formula: `Managed Agents fee = Model invocation fee + agent runtime fee + tool calling fee`

## Coding Plan

### Pricing

|Plan |Subscription duration |Price |
|---|---|---|
|Lite |1 month<br><br>3 months |$10<br><br>$30 |
|Pro |1 month<br><br>3 months |$50<br><br>$150 |

For plan information and special offers, see [Subscription overview](https://ai.byteplus.com/ark/region:ap-southeast-1/docs/coding-plan-personal-plan-overview).

## Rate limits

The call limits for each model are requests per minute (RPM) and tokens per minute (TPM). You can go to the [Model activation](https://ai.byteplus.com/ark/region:ap-southeast-1/openManagement) page to view the limits of each account and all its sub\-accounts.

To increase your limits, submit a request on the console.

## Billing notes

* An account is considered in arrears when its balance becomes insufficient or falls below zero.

* If an account stays in arrears for 2 hours, the model services will be suspended. Services will resume once the account has been recharged. Please make sure that all outstanding bills are paid on time to avoid any unexpected service interruption.
