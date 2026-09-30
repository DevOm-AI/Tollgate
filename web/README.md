# Tollgate dashboard

React + TypeScript (Vite). Per key: spend against budget, requests and tokens per day, p50/p95
latency, error rate by provider, and the request log (metadata only). The admin view creates
keys, changes limits and revokes keys.

It talks to Tollgate's `/admin` API with the admin key, which is kept for the browser tab only
(sessionStorage). Tollgate must allow the dashboard's origin in `CORS_ORIGINS`.

```bash
npm install
npm run dev        # http://localhost:5173, against http://localhost:8001 by default
npm test
npm run build      # static files in dist/
```

`VITE_API_URL` sets the API address shown on the sign-in page.
