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
# Updated 2026-08-08 evening with v2 conv-stem campaign (scripts/verify_claims.py: 16/16).

18. lob-transformer — Trained transformers on 254k limit-order-book snapshots (FI-2010) to predict short-horizon mid-price direction: conv-stem transformer reaches 0.70 macro F1 vs 0.65 plain transformer at an equal 60-epoch budget (3 seeds each, non-overlapping ranges) and 0.36/0.27 MLP/logistic baselines under a fixed walk-forward protocol (PyTorch, Apple-Silicon MPS)
19. lob-transformer — Found and fixed a data-leakage bug in the standard FI-2010 setup (files silently concatenate 5 stocks; naive sliding windows cross stock boundaries), with segment detection and split hygiene enforced by construction and 34 unit tests
20. lob-transformer — Isolated architecture from training budget with a fair-budget control: 4x longer training bought +4.2 F1 alone, DeepLOB-style conv stem added +5.4 on top; every published number re-derived from committed JSON artifacts by an automated claim verifier (16/16)

# Bullets 21-24 approved 2026-08-08. Source: raft-kv repo results/chaos_summary.json,
# mutation_summary.json, m1_deep_hunt.json, figure8_summary.json (all committed).
# NOTE: 24 is a compact alternative to 21+23 — use 21+23 OR 24, never all three together.
# 22 works as an optional third line alongside either choice.

21. raft-kv — Implemented Raft consensus + linearizable KV store as a pure state machine verified by deterministic simulation: 1000 seeded chaos schedules (1,674 crash-restarts, 1,491 partitions, 10% msg loss, ~941k messages) with 5 safety invariants checked every tick and zero violations
22. raft-kv — Built Wing-Gong linearizability checker validating 51k client operations across chaos schedules against a sequential put/get/CAS spec, with exactly-once client sessions; checker itself unit-tested to reject stale reads, lost updates, and lying CAS results
23. raft-kv — Used mutation testing to measure test-suite power: 3 of 4 injected Raft bugs caught within 3 random schedules, but the Figure 8 commit bug (§5.4.2) survived 5,000 — closed the gap with a scripted adversarial interleaving that triggers state-machine divergence deterministically
24. raft-kv (compact variant of 21+23) — Raft + linearizable KV verified by deterministic simulation: 1000 seeded chaos schedules, 5 invariants per tick, 0 violations; mutation testing exposed that random fault injection misses the Figure 8 bug in 5,000 schedules, fixed with a scripted adversarial scenario

# Bullets 25-28 approved 2026-08-08. Source: zonal-ecu-sim repo (github.com/david-cui-bruno/zonal-ecu-sim),
# tests/run_tests.sh output (all suites green), README. C11, -Wall -Wextra -Werror, no dynamic allocation.
# NOTE: 28 is a compact variant of 25+26 — use 25+26+27 OR 27+28, never 25/26/28 together.

25. zonal-ecu-sim — Built a miniature software-defined-vehicle network in C11: four ECUs (BMS, vehicle controller, sensor, zonal gateway) exchanging CRC-8/rolling-counter-protected CAN-FD frames over an emulated bus, with signal layouts code-generated from a DBC file and cross-validated byte-for-byte against cantools
26. zonal-ecu-sim — Implemented UDS (ISO 14229) diagnostics over ISO-TP on a zonal gateway: sessions with S3 timeout, seed/key security access, DTC read/clear derived from live fault flags, multi-frame transfers, plus allowlist routing and token-bucket rate limiting isolating the untrusted diag bus (verified by adversarial injection tests: zero frames leaked)
27. zonal-ecu-sim — Verified fault handling end-to-end with live fault injection: thermal-runaway drill latches BMS contactors open and drops the VCU to limp mode with torque gated to zero; dead-sensor drill caps torque at 30% and recovers; diagnosed and fixed a head-of-line-blocking bus bug where one stalled client wedged the network (non-blocking fan-out, per-client queues, overrun drop)
28. zonal-ecu-sim (compact variant of 25+26) — Four-ECU vehicle network in C11 over emulated CAN-FD: DBC-generated signal packing (cross-validated vs cantools), UDS/ISO-TP diagnostics with seed/key security, allowlist gateway isolating an untrusted diag bus, all behavior verified by live integration + adversarial injection tests

# Bullets 29-32 approved 2026-08-09. Source: canary-operator repo
# (github.com/david-cui-bruno/canary-operator): envtest suite (6 scenarios) +
# real kind-cluster acceptance run documented in README.
# NOTE: 31 is a compact variant of 29+30 — use 29+30 OR 31, never all three.

29. canary-operator — Built a Kubernetes operator (Go, controller-runtime) for progressive canary rollouts: gated step schedule shifts replicas to a canary Deployment while holding total serving capacity invariant, with per-step progress deadlines and pod-restart budgets triggering automatic rollback
30. canary-operator — Designed crash-safe reconciliation where CRD status + cluster state fully determine every decision (deterministic canary adoption, finalizer cleanup); verified with 6 integration scenarios against a real kube-apiserver plus an end-to-end kind-cluster run covering promotion and ImagePullBackOff rollback
31. canary-operator (compact variant of 29+30) — Kubernetes operator (Go) for canary rollouts: capacity-invariant step schedule, health-gated auto-rollback, crash-safe state derivation; envtest-verified (6 scenarios vs real kube-apiserver) plus live kind-cluster promotion and rollback runs
32. canary-operator — Integration test caught a real reconciler bug pre-release: health gates stopped firing while rollouts were parked at pause steps (pod restarts don't bump owned-Deployment generation); fixed with bounded requeues and locked in by the failing-then-passing test

# Bullets 33-36 approved 2026-08-09. Source: metal-kernels repo
# (github.com/david-cui-bruno/metal-kernels), results/*.json committed;
# bench/verify_claims.py re-derives all 28 README numbers (28/28).
# NOTE: 35 is a compact variant of 33+34 — use 33+34 OR 35, never all three.

33. metal-kernels — Hand-wrote Metal compute kernels on Apple Silicon: matmul progression from naive (598 GF/s) to threadgroup-tiled (1,082) to simdgroup-matrix hardware (2,512 GF/s, 4.2x), reaching 42% of Apple's closed-source MPS GEMM; every output cross-validated byte-identically against torch.mm
34. metal-kernels — Built a fused softmax kernel (per-row threadgroup parallel reductions, 3 memory trips vs 5) that beats torch.softmax on every tested shape (1.2-2.4x) and sustains 363 GB/s effective bandwidth at 16k columns, with overflow-range and small-shape numerics verified to <3e-8
35. metal-kernels (compact variant of 33+34) — Metal GPU kernels on Apple Silicon: matmul naive->tiled->simdgroup (4.2x, 42% of MPS) and fused softmax beating torch.softmax up to 2.4x; outputs cross-validated against PyTorch, GPU-timestamp benchmarks with committed JSON artifacts
36. metal-kernels — Benchmarked with GPU-side timestamps (median-of-5, warmup excluded) against PyTorch MPS baselines on identical shapes; automated claim verifier re-derives all 28 published numbers from committed artifacts and fails CI-style on drift
