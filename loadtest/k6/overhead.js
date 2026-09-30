// POSTs a chat completion as fast as TARGET allows, from VUS virtual users.
//   k6 run -e TARGET=http://127.0.0.1:9100/v1/chat/completions -e KEY=tg_live_... overhead.js
import http from "k6/http";
import { check } from "k6";

export const options = {
  vus: Number(__ENV.VUS || 10),
  duration: __ENV.DURATION || "20s",
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
};

const body = JSON.stringify({
  model: "mock",
  messages: [{ role: "user", content: "Say hello" }],
  max_tokens: 32,
});
const params = {
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${__ENV.KEY || "none"}` },
};

export default function () {
  const response = http.post(__ENV.TARGET, body, params);
  check(response, { "status is 200": (r) => r.status === 200 });
}
