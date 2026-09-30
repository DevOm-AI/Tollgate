"""The mock provider on its own, as a plain HTTP server: the "no gateway" baseline.

    uvicorn loadtest.mock_server:app --port 9100

It answers POST /v1/chat/completions with exactly what Tollgate's mock provider returns, with
no delay, so timing it directly and through Tollgate isolates what Tollgate adds.
"""

from fastapi import FastAPI

from app.providers.base import ChatCompletion, ChatCompletionRequest
from app.providers.mock import MockProvider

app = FastAPI(title="Mock provider")
mock = MockProvider(delay_ms=0, output_tokens=32)


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest) -> ChatCompletion:
    return await mock.complete(request)
