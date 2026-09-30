import httpx2

# One pooled client for every provider call. Lazy: nothing connects until the first request.
http_client = httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=5.0))
