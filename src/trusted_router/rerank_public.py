"""Public schema for the gateway's synchronous rerank operation."""

from typing import Any

_TEXT_DOCUMENT = {
    "type": "object",
    "required": ["text"],
    "additionalProperties": False,
    "properties": {"text": {"type": "string"}},
}

RERANK_OPENAPI: dict[str, Any] = {
    "summary": "Rerank text documents",
    "description": (
        "Voyage rerank models on the GCP API hosts: the default api.trustedrouter.com "
        "and EU regional api-europe-west4.quillrouter.com. Not yet served on the AWS "
        "or Azure API hosts. Synchronous, prepaid credits only, at the model's listed "
        "price per input token: the query is counted once per document plus every document. "
        "Not available through Batch, the confidential-only hostname, or a customer's "
        "own provider key. Image documents are refused. The control-plane host does not serve it."
    ),
    "security": [{"BearerAuth": []}],
    "requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object",
        "required": ["model", "query", "documents"],
        "properties": {
            "model": {"type": "string", "description": "A rerank model ID from GET /v1/rerank/models."},
            "query": {"type": "string", "minLength": 1},
            "documents": {
                "type": "array", "minItems": 1, "maxItems": 1000,
                "items": {"oneOf": [{"type": "string"}, _TEXT_DOCUMENT]},
            },
            "top_n": {"type": "integer", "minimum": 1, "description": "Capped at the document count."},
            "return_documents": {"type": "boolean", "default": True},
            "user": {"type": "string", "description": "Same attribution member as embeddings."},
            "session_id": {"type": "string", "description": "Same attribution member as embeddings."},
            "provider": {"type": "object", "description": "Embeddings provider preferences, limited to prepaid credits."},
        },
    }}}},
    "responses": {"200": {"description": "Documents sorted by relevance_score descending, ties by index, cut to top_n.",
        "content": {"application/json": {"schema": {
            "type": "object", "required": ["id", "model", "provider", "results", "usage"],
            "properties": {
                "id": {"type": "string"},
                "model": {"type": "string"},
                "provider": {"type": "string"},
                "results": {"type": "array", "items": {
                    "type": "object", "required": ["index", "relevance_score"],
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "relevance_score": {"type": "number"},
                        "document": {**_TEXT_DOCUMENT, "description": "Text from the request; omitted when return_documents is false."},
                    },
                }},
                "usage": {
                    "type": "object", "required": ["total_tokens"],
                    "description": "Total input tokens and the same cost fields as the embeddings response.",
                    "properties": {"total_tokens": {"type": "integer", "minimum": 0}},
                },
            },
        }}}},
        "501": {"description": "Use the GCP API origin for inference; the control plane does not serve this request."},
    },
}
