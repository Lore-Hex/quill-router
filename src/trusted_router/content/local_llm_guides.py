"""Editorial guides for teams evaluating local inference and hosted alternatives."""

from typing import NamedTuple


class LocalGuide(NamedTuple):
    slug: str
    title: str
    introduction: str
    exercise: str
    decision: str


LOCAL_GUIDES = (
    LocalGuide(
        'local-llm-getting-started',
        'Should you run your first LLM locally?',
        'Start with a task you can recognize as correct. A useful first experiment is to turn a '
        'fictional meeting transcript into decisions, owners and deadlines.',
        'Give the model five short transcripts, including one where nobody agreed to a deadline. '
        'Count invented commitments as errors. Save the model version and prompt so a second run '
        'means something.',
        'Choose local inference when offline use matters and your machine finishes at a '
        'comfortable speed. For occasional access to a larger model, compare the same five '
        'outputs before buying hardware.',
    ),
    LocalGuide(
        'local-llm-memory-budget',
        'How much memory does a local LLM need?',
        'Estimate weight memory first. Multiply the parameter count by bits per weight and divide '
        'by eight. That gives a lower bound in bytes before caches, runtime buffers and '
        'quantization metadata.',
        'An 8-billion-parameter model at four bits has roughly 4 GB of raw weights. Load the '
        'actual artifact and measure free memory again after a long prompt. Repeat at the '
        'concurrency your application needs.',
        'Choose a configuration that leaves room for the operating system and the largest '
        'expected request. A successful load alone tells you little about sustained use.',
    ),
    LocalGuide(
        'local-llm-quantization',
        'Which quantization should you download?',
        'Quantizing weights reduces their footprint. The decision turns on how much task accuracy '
        'you give up for the memory and speed you gain.',
        'Download two quantizations of the same checkpoint. Ask both to extract dates, quantities '
        'and exceptions from twenty short records. Use the same prompt, context and decoding '
        'settings. Count changed digits separately from wording differences.',
        'Keep the smaller artifact if both pass the meaningful checks. When the smaller one drops '
        'a critical field, repeat with a higher precision before changing model families.',
    ),
    LocalGuide(
        'local-llm-gpu-or-cpu',
        'Does a local LLM need a GPU?',
        'A CPU can be enough for an overnight batch even when it feels slow in a chat window. '
        'Measure the time limit your work actually imposes.',
        'If 600 records must finish in ten hours, your budget is one minute per record, including '
        'reading and writing. Time twenty representative records, then repeat while your normal '
        'desktop applications are running.',
        'Buy acceleration when the measured runtime or interactive delay misses your target. For '
        'a batch that runs once a quarter, price the same job through an API first.',
    ),
    LocalGuide(
        'local-llm-mac-unified-memory',
        'How do you size a Mac for local inference?',
        'Apple silicon shares memory between the CPU, GPU and applications. Size a machine for '
        'the combined workload you intend to keep open.',
        'Load the model while your editor, browser and document store are running. Watch memory '
        'pressure and swap during the longest conversation you expect. Compare the second and '
        'tenth answers to the first.',
        'Choose additional unified memory if the working set causes sustained swapping. More '
        'storage makes room for downloads but does not provide the same working memory.',
    ),
    LocalGuide(
        'local-llm-gpu-offload',
        'What if the model only partly fits in VRAM?',
        'Some runtimes can place part of a model on the GPU and keep the rest in system memory. A '
        'hybrid load can make a larger model usable, with a throughput cost that depends on the '
        'machine.',
        'In Ollama, run <code>ollama ps</code> during generation and inspect the processor split. '
        'Time the same answer with a smaller fully GPU-resident model. Record answer quality as '
        'well as seconds. <a href="https://docs.ollama.com/faq">Ollama local runtime FAQ</a>.',
        'Use partial offload if the quality gain is worth the delay on your inputs. If users '
        'abandon the request, the larger model is a poor interactive choice even when it fits.',
    ),
    LocalGuide(
        'local-llm-gguf',
        'How do you choose the right GGUF file?',
        'GGUF packages model tensors and metadata for compatible runtimes such as llama.cpp. A '
        'filename can combine a model revision, parameter size and quantization. Check each '
        'independently.',
        'Before downloading, record the publisher, upstream checkpoint, license, file size and '
        'supported architecture. Verify any published checksum. Run one prompt from the model '
        'card to catch a wrong template or incompatible build.',
        'Keep the artifact identifier beside your evaluation results. Two files bearing the same '
        'family name can have different weights, formats and behavior. '
        '<a href="https://github.com/ggml-org/llama.cpp">llama.cpp model formats</a>.',
    ),
    LocalGuide(
        'local-llm-ollama-or-llama-cpp',
        'Ollama or llama.cpp for a local service?',
        'Choose based on who will operate the service. Ollama provides model management and a '
        'local API. llama.cpp is useful when you want direct control over a compatible model file '
        'and runtime settings.',
        'Give a colleague a clean machine and your setup notes. Ask them to install the exact '
        'model, serve a request, restart the service and reproduce the result. Record every '
        'undocumented manual step.',
        'Use the setup your team can reproduce. Keep the model artifact and context settings '
        'fixed when comparing runtime performance.',
    ),
    LocalGuide(
        'local-llm-context-window',
        'How much context should a local model reserve?',
        'Budget for system instructions, conversation history, retrieved text and the answer. A '
        "model's advertised maximum is only one limit. Memory and prompt-processing time can "
        'impose a smaller practical budget.',
        'For a 16,384-token runtime window, an 11,000-token document plus 2,000 tokens of '
        'instructions and history leaves 3,384 tokens before template overhead. Reserve the '
        'answer before filling the rest with source text.',
        'Test with a fact near the end of the document. If the answer misses it, inspect '
        'truncation and retrieval before paying for a larger window.',
    ),
    LocalGuide(
        'local-llm-kv-cache',
        'Why does local inference use more memory as chats grow?',
        'During autoregressive generation, the runtime can retain attention keys and values for '
        'prior tokens. This cache helps avoid recomputation and consumes memory alongside model '
        'weights.',
        'Measure memory with one short chat, one long chat and four concurrent long chats. Keep '
        'the checkpoint and weight quantization fixed. Record whether your runtime preallocates '
        'the configured window.',
        'Use these observations to cap context or concurrent sessions. Reducing weight size alone '
        'may leave the long-chat memory problem untouched.',
    ),
    LocalGuide(
        'local-llm-latency',
        'How fast will a local model feel in chat?',
        'Separate time to first token from the speed of the remaining answer. Long prompts can '
        'make an otherwise fast generator feel unresponsive.',
        'Time an empty-history question, then repeat after adding a representative document. '
        'Record first visible token, final token and output count. Label cold and warm results '
        'separately.',
        'Optimize the slower stage. Shortening retrieved context can help an expensive prompt, '
        'while a shorter answer budget can improve a long generation.',
    ),
    LocalGuide(
        'local-llm-throughput',
        'How many people can share a local LLM?',
        'Single-user tokens per second cannot predict queue delays when colleagues arrive '
        'together. Define acceptable wait time per user before sizing the server.',
        'Replay the same mix at one, two and four simultaneous sessions. Track completed jobs per '
        'minute, the slowest requests and any memory failures. Keep a few long prompts in the '
        'mix.',
        'Choose the smallest setup that meets your busy-period target. If traffic is concentrated '
        'in short bursts, compare overflow cost with idle capacity.',
    ),
    LocalGuide(
        'local-llm-power-cost',
        'What does a local LLM cost in electricity?',
        "Measure whole-system power over the actual job. A GPU's advertised power limit is a poor "
        'estimate of energy consumed per completed answer.',
        'At an illustrative 300 watts for four hours, the system uses 1.2 kWh. At 25 cents per '
        'kWh that is 30 cents. Divide by accepted outputs and account separately for idle hours '
        'if the server stays on.',
        'Use your meter and tariff before extrapolating. Include hardware and maintenance in the '
        'purchase decision, even when the incremental electricity bill is small.',
    ),
    LocalGuide(
        'local-llm-buy-or-rent',
        'When is buying a GPU cheaper than API inference?',
        'Compare monthly fixed costs with the hosted bill for the same useful work. Count input '
        'and output tokens separately because their prices can differ.',
        'Suppose ownership costs 90 dollars a month before energy and a hosted job costs 10 '
        'cents. At 900 jobs the hosted total reaches 90 dollars. Power, retries and support time '
        'move the actual break-even point.',
        'Model a quiet month as well as a busy one. A purchase that pays off only at peak demand '
        'may leave expensive capacity idle.',
    ),
    LocalGuide(
        'local-llm-batch-jobs',
        'Should an overnight batch run on a local LLM?',
        'A batch has a deadline, an input count and a quality threshold. Those numbers are more '
        'useful than a peak generation benchmark.',
        'For 12,000 rows in eight hours, the pipeline must finish 25 rows per minute. Benchmark '
        'reading, preprocessing, inference, validation and output writes together. Include '
        'retries of malformed answers.',
        'Make each row restartable with a stable job ID. Rent extra capacity only for material '
        'your policy allows off-device and only if the batch will otherwise miss its deadline.',
    ),
    LocalGuide(
        'local-llm-offline',
        'How do you verify that a local LLM works offline?',
        'Offline operation depends on the whole application. A locally stored checkpoint can '
        'still be paired with a remote embedding service, tool, login requirement or telemetry '
        'integration.',
        'Download the permitted artifacts, disconnect networking, restart the app and open a new '
        'document. Test retrieval, generation and reopening a saved conversation. Record which '
        'steps still need connectivity.',
        'Use this test before taking a workstation to a disconnected site. For strictly offline '
        'work, keep hosted inference disabled.',
    ),
    LocalGuide(
        'local-llm-document-privacy',
        'Where can private documents go in a local LLM app?',
        'Trace a document through ingestion, embeddings, retrieval, generation and logging. Each '
        'stage can have a different storage location or service owner.',
        "Put a unique fictional marker in a test document. Inspect the application's configured "
        'index, chat history, backups and permitted logs. Delete the document and check whether '
        'the marker remains retrievable.',
        'Approve each stage for the data class it handles. An encrypted remote inference route is '
        'a separate choice when organizational policy permits remote processing.',
    ),
    LocalGuide(
        'local-llm-rag',
        'Should you use local RAG or a longer context window?',
        'Retrieval-augmented generation selects passages before the model answers. It is useful '
        'when the collection changes often or only a small fraction is relevant to each question.',
        'Build ten questions with known source passages. First check whether retrieval returns '
        'the right passage. Then give the model the correct passage directly. This separates '
        'retrieval errors from answer errors.',
        'Improve retrieval when the evidence is missing. Try a longer window when the evidence '
        'arrives but spans sections that cannot fit together.',
    ),
    LocalGuide(
        'local-llm-embeddings',
        'Which embedding model works for local document search?',
        "Choose embeddings by how often the right passage appears near the top for your users' "
        'language and terminology. Vector dimensions alone do not answer that question.',
        'Create queries that use different words from the source, such as “leave allowance” for a '
        'policy titled “paid time off.” Compare the first five passages returned by each '
        'candidate using the same chunks.',
        'Version the embedding model with the index. When you change the embedding model, rebuild '
        'the document vectors before comparing query results.',
    ),
    LocalGuide(
        'local-llm-chunking',
        'How large should chunks be for a local knowledge base?',
        'A useful chunk carries enough context to interpret a fact. Tiny chunks can separate a '
        'rule from its exception, while large chunks use prompt space on unrelated material.',
        'Try heading-based chunks on a policy manual. Ask for a limit whose exception appears in '
        'the following paragraph. Compare retrieval with and without a small overlap, keeping the '
        'total token budget fixed.',
        'Keep the segmentation that returns both the rule and its exception with a traceable '
        'source. Record the original page or section alongside each chunk.',
    ),
    LocalGuide(
        'local-llm-structured-output',
        'Can a local LLM reliably return JSON?',
        'Treat machine readability and factual correctness as separate checks. A syntactically '
        'valid object may still contain a fabricated field or an amount in the wrong unit.',
        'Start with fictional invoices and a schema for currency, amount and date. Include one '
        'missing date and one invoice with two amounts. Validate the schema, then compare each '
        'field with your reference answer.',
        'Specify how missing values should appear. If constrained decoding is available, test it '
        'with the same examples and keep application-side validation.',
    ),
    LocalGuide(
        'local-llm-tool-use',
        'How do you test tool calling with a local model?',
        'Tool calling has two parts: the model proposes an action and your application decides '
        'whether to execute it. Test the proposal before connecting real tools.',
        'Give the model a read-only mock inventory with three product IDs. Ask about one item, '
        'then about an item that does not exist. Check the selected tool, argument types and '
        'whether the model invents an ID.',
        'Choose a tool-capable checkpoint and compatible chat template. Allowlist tool names and '
        "validate arguments separately from the model's explanation.",
    ),
    LocalGuide(
        'local-llm-coding',
        'Is a small local coding model good enough for your repository?',
        'Use a real maintenance task with a testable outcome. A plausible-looking patch is a weak '
        'measure if it calls an API your project never used.',
        'Freeze a small revision, provide the same files and ask each candidate to correct a '
        'formatting bug. Run the existing test and inspect how many unrelated lines changed. '
        'Repeat on a second language if your repository is mixed.',
        'Track time to an accepted patch, including review. A faster generator can cost more '
        'developer time if its output often needs rewriting.',
    ),
    LocalGuide(
        'local-llm-summarization',
        'Which local LLM should summarize meeting notes?',
        'Score the facts a reader will act on: who owns a task, what was decided and whether a '
        'deadline was actually agreed. Fluency can hide missing or invented decisions.',
        'Write a reference list from a fictional transcript with a proposal that gets rejected '
        'halfway through. Ask for decisions and unresolved questions separately. Mark each '
        'unsupported assignment or date.',
        'Choose the smallest model that preserves those distinctions. For long meetings, carry '
        'source timestamps through chunk summaries so reviewers can check the evidence.',
    ),
    LocalGuide(
        'local-llm-multilingual',
        'How do you evaluate a multilingual local LLM?',
        'Evaluate documents written naturally in each target language. Translated English '
        'benchmarks can miss regional terms, names and differences in formality.',
        'Have a fluent reviewer select ten representative messages per language. Include a local '
        'date format, a mixed-language sentence and a domain-specific abbreviation. Assess '
        'meaning and tone separately.',
        'Compare latency and token counts for each language too. A workload that fits comfortably '
        'in one language may use more of the context window in another.',
    ),
    LocalGuide(
        'local-llm-model-license',
        'What should you check before using model weights commercially?',
        'Start with the exact artifact you plan to deploy. A base model, fine-tune and repackaged '
        'quantization can carry separate notices and usage conditions.',
        'Build a release record with the upstream revision, fine-tune publisher, license files '
        'and intended use. Review distribution separately if you will put the weights on a '
        "customer's machine.",
        'Send unresolved terms to your licensing reviewer before deployment. Buying hosted '
        'inference changes who operates the hardware, while the service and model terms still '
        'matter.',
    ),
    LocalGuide(
        'local-llm-model-upgrades',
        'How do you upgrade a local LLM without breaking an app?',
        'A new checkpoint can change tokenization, formatting and tool behavior even when its '
        'public name looks familiar. Keep the previous artifact available during evaluation.',
        'Replay stored fictional fixtures against both revisions. Compare JSON validity, answer '
        'accuracy, response length and latency. Run one rollback rehearsal with the old prompt '
        'template and tokenizer.',
        'Promote the revision only after the application passes its acceptance tests. Record the '
        'model digest, runtime build and prompt version together.',
    ),
    LocalGuide(
        'local-llm-fine-tune-or-retrieve',
        'Should you fine-tune a local LLM or add retrieval?',
        'Use retrieval when the answer depends on changing documents. Consider a fine-tune when '
        'the inputs contain enough information but the model repeatedly misses a stable format or '
        'behavior.',
        'For an internal support assistant, supply the correct manual passage directly. If the '
        'answer becomes accurate, test retrieval first. If formatting still fails on many '
        'examples, compare prompt changes before training.',
        'Keep a held-out set from different documents or customers. Judge improvements on that '
        'set before budgeting for training and long-term model maintenance.',
    ),
    LocalGuide(
        'local-llm-hybrid-routing',
        'How can a local LLM use encrypted cloud overflow?',
        'Define which requests can leave the machine before enabling overflow. Confidential '
        'inference can help with approved data, while a local-only category must stay local even '
        'during an outage.',
        'Label three synthetic jobs: local only, approved for remote confidential compute, and '
        'public. Simulate a full queue and a failed remote call. Log the routing decision without '
        'logging prompt content.',
        'Allow the remote branch only for approved jobs and fail closed when no qualifying '
        'endpoint exists. Verify that SDK retries cannot select a less restrictive endpoint.',
    ),
    LocalGuide(
        'local-llm-api-cost',
        'How much would an encrypted API cost for your local workload?',
        "Estimate cost using the eligible route's actual input and output prices. A cheap catalog "
        'route with a different privacy tier is a different purchase.',
        'For 10,000 calls averaging 800 input and 200 output tokens, multiply the quoted '
        'per-million input price by eight and the output price by two. Add retries and compare '
        'with the cost of running your machine for the same jobs.',
        'Run a small paid sample and compare usage receipts with your estimate. Savings depend on '
        'workload, route, utilization and the number of answers accepted.',
    ),
)
