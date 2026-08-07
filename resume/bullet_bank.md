# Bullet Bank — APPROVED 2026-08-04 (all 12; metric-injected)
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
