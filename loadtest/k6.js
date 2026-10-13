// Load test: ramps to 2,000 requests/second spread across 500 simulated clients.
//
//   k6 run loadtest/k6.js                                  (local compose stack)
//   k6 run -e BASE_URL=https://your-app.onrender.com loadtest/k6.js
//
// 429s are the limiter doing its job, so they count as successes here.
// A "failure" means a 5xx or a timeout.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:8080";
const ALGOS = ["fixed_window", "sliding_log", "sliding_window_counter", "token_bucket"];
const CLIENTS = 500;

const allowed = new Counter("rl_allowed");
const limited = new Counter("rl_limited");

http.setResponseCallback(http.expectedStatuses(200, 429));

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-arrival-rate",
      startRate: 100,
      timeUnit: "1s",
      preAllocatedVUs: 200,
      maxVUs: 2000,
      stages: [
        { target: 500, duration: "30s" },
        { target: 2000, duration: "1m" },
        { target: 2000, duration: "1m" },
        { target: 0, duration: "15s" },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<50", "p(99)<100"],
  },
};

export default function () {
  const algo = ALGOS[Math.floor(Math.random() * ALGOS.length)];
  const client = `k6-${Math.floor(Math.random() * CLIENTS)}`;
  const res = http.get(`${BASE}/api/demo/${algo}`, {
    headers: { "X-Client-Id": client },
    tags: { algo },
  });
  check(res, {
    "200 or 429": (r) => r.status === 200 || r.status === 429,
    "has RateLimit headers": (r) => r.headers["Ratelimit-Limit"] !== undefined,
  });
  (res.status === 200 ? allowed : limited).add(1);
}
