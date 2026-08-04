# Bullet Bank — DRAFT, pending David's approval
#
# Rules: lines starting with [PENDING] are NOT used by the tailor until David
# approves them (reply/tell the agent "approve bullet N" or edit this file and
# delete the tag). Approved bullets are fair game for any tailored resume.
# The tailor may lightly reword approved bullets to match a JD's vocabulary.

## Framewise Health (Co-Founder & CTO)

1. [PENDING] Owned end-to-end infrastructure: AWS deployment, Supabase Postgres with RLS, S3 media storage, CI/CD, and monitoring for patient-facing production apps with PHI-sensitive data handling
2. [PENDING] Ran weekly forward-deployed sessions with hospital discharge nurses and clinic staff, converting observed workflow friction directly into shipped product changes
3. [PENDING] Designed HIPAA-conscious data model separating PHI from analytics events, enabling engagement dashboards without exposing patient identity
4. [PENDING] Interviewed and evaluated design partners and pilot clinics, driving product decisions from clinician feedback loops under YC-pace iteration

## Freya (SWE Intern)

5. [PENDING] Built retry/fallback orchestration across STT/LLM/TTS vendors, improving call completion rates and isolating provider outages without dropping live calls
6. [PENDING] Instrumented latency tracing across the voice pipeline (SIP ingress -> STT -> LLM -> TTS) to locate and eliminate tail-latency spikes
7. [PENDING] Wrote load-testing harness simulating hundreds of concurrent SIP/WebRTC calls to validate scaling behavior before customer launches

## Sotatek (SWE Intern)

8. [PENDING] Built feature-engineering pipeline (rolling aggregates, velocity features) feeding the CatBoost fraud model, versioned and backtestable
9. [PENDING] Added drift monitoring and weekly retraining jobs for the fraud model, keeping precision stable as transaction patterns shifted

## Projects (candidates to swap in per-JD)

10. [PENDING] jobhunt — Autonomous job-application pipeline: watchers diff GitHub listing repos, Claude tailors LaTeX resumes per JD, Playwright adapters submit across Greenhouse/Lever/Ashby/Workday with a Gmail human-in-the-loop approval flow (Python, Playwright, SQLite, launchd)
11. [PENDING] SpaceOverflow marketplace scaling bullet: Stripe Connect marketplace flows with idempotent webhook processing and real-time messaging via Supabase channels
12. [PENDING] Brown slang dictionary (brunos-dictionary): Next.js 14 + Supabase app with auth, moderated submissions, and type-safe DB migrations; deployed on Vercel
