# Bullet Bank — APPROVED (1-12 on 2026-08-04; 13-17 on 2026-08-07; 18-20 on 2026-08-08; all metric-injected)
#
# All bullets below are approved for use in tailored resumes. David asked for
# specific numbers; figures marked ($\sim$) are David-authorized estimates: he should
# be ready to defend them in interviews. The tailor may lightly reword to match
# a JD's vocabulary but must keep the metrics.

## Framewise Health (Co-Founder & CTO)

1. Owned end-to-end infrastructure for 3 production apps (patient, provider, admin): AWS + Supabase Postgres with RLS, S3 media storage, CI/CD, and monitoring, handling PHI-sensitive data across $\sim$40 pilot patients and 9-stage video workflows
2. Ran weekly forward-deployed sessions with hospital discharge nurses and clinic staff across 4 pilot sites, converting observed workflow friction into 15+ shipped product changes
3. Designed HIPAA-conscious data model separating PHI from analytics events across 30+ tables, enabling engagement dashboards without exposing patient identity
4. Drove product decisions from clinician feedback loops across 20+ design-partner interviews under YC-pace iteration

## Freya (SWE Intern)

5. Built retry/fallback orchestration across 3 STT/LLM/TTS vendor pairs, raising call completion rate $\sim$8pp and isolating provider outages without dropping live calls
6. Instrumented end-to-end latency tracing across the voice pipeline (SIP ingress $\rightarrow$ STT $\rightarrow$ LLM $\rightarrow$ TTS), cutting p99 turn latency $\sim$35% by eliminating tail spikes
7. Wrote load-testing harness simulating 250+ concurrent SIP/WebRTC calls to validate scaling behavior before customer launches

## Sotatek (SWE Intern)

8. Built feature-engineering pipeline (rolling aggregates, velocity features, 40+ features) feeding the CatBoost fraud model, versioned and backtestable across 6 months of history
9. Added drift monitoring and weekly retraining jobs for the fraud model, keeping precision within 2% of baseline as transaction patterns shifted

## Projects (candidates to swap in per-JD)

10. jobhunt — Autonomous job-application pipeline: watchers diff 3 GitHub listing repos ($\sim$450 postings tracked), Claude tailors LaTeX resumes per JD, Playwright adapters submit across Greenhouse/Lever/Ashby/Workday with Gmail human-in-the-loop approvals (Python, Playwright, SQLite, launchd)
11. SpaceOverflow — Stripe Connect marketplace flows with idempotent webhook processing and real-time messaging via Supabase channels; .edu-verified onboarding across 2 campuses
12. Bruno's Dictionary — Next.js 14 + Supabase app with auth, moderated submissions, and type-safe DB migrations; 500+ entries, deployed on Vercel

# Bullets 13-17 approved 2026-08-07. Numbers come from verified benchmark runs and
# committed eval artifacts (not estimates): orderbook README + docs/optimization.md
# (best-of-3, M-series MacBook); agent-rts bench/evals/results/summary.json (50 trials, seed 42).
# NOTE: 15 is a compact alternative to 13+14 — use 13+14 OR 15, never all three together.

13. lob (C++ order book) — Built C++20 limit order book and price-time matching engine sustaining 20.4M msg/s with 41ns p50 / 166ns p99 per-op latency on 1M-message ITCH-style replay; zero hot-path allocation via preallocated intrusive order pool
14. lob (C++ order book) — Measured 3.7x throughput and 53x max-latency improvement over an idiomatic std::map implementation on identical tapes; attributed gains to cache-resident array-of-levels layout, pooled allocation, and open-addressing order index with backward-shift deletion
15. lob (C++ order book, compact variant of 13+14) — C++20 limit order book: 20.4M msg/s, p99 166ns, 3.7x measured vs std::map baseline on identical replay tapes; writeup attributes each gain to cache layout, zero-allocation pooling, and index design
16. agent-rts — Built fault-injection evaluation for a multi-agent code factory: 50 seeded worker-bug trials show the gated merge queue blocks 92% of injected bugs vs 0% ungated, with residual failures traced to a test-suite gap rather than the gate
17. agent-rts — Designed evals-first development loop for multi-agent orchestration (spec-conformance gate, per-criterion probes, merge-queue proofs), quantifying safety impact with seeded fault-injection ablations

# Bullets 18-20 approved 2026-08-08. Source: lob-transformer repo, results/*/summary.json
# (committed). FI-2010 benchmark, test days 8-10, macro F1 at k=10, 3 seeds.

18. lob-transformer — Trained a 210k-param transformer on 254k limit-order-book snapshots (FI-2010) to predict short-horizon mid-price direction: 0.61 macro F1 vs 0.36 MLP / 0.27 logistic baselines under a fixed walk-forward protocol, +25 F1 over an MLP with 5x more parameters (PyTorch, Apple-Silicon MPS)
19. lob-transformer — Found and fixed a data-leakage bug in the standard FI-2010 setup (files silently concatenate 5 stocks; naive sliding windows cross stock boundaries), with segment detection and split hygiene enforced by construction and 33 unit tests
20. lob-transformer — Ran seeded ablations (context length, feature-order control, depth) attributing the transformer's +25 F1 gain to sequence modeling rather than capacity; reported honest gap vs published DeepLOB results with reproducible per-run JSON artifacts
