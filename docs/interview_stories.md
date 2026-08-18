# David Cui — Behavioral Interview Story Bank (STAR)

---

## Story 1: Leadership — Founding CTO Across 4 Pilot Sites

**Best for:** Leadership, ownership, ambiguity, 0-to-1 roles, healthcare/regulated-industry questions

**Situation:** As co-founder and CTO of Framewise, a 2-person YC healthcare startup, I was responsible for all technical infrastructure and product direction across three production apps (patient, provider, admin) serving ~40 pilot patients across 4 hospital/clinic sites, with PHI-sensitive data and 9-stage video workflows.

**Task:** I needed to build infrastructure that could hold up under HIPAA constraints while also making sure the product actually matched how discharge nurses and clinic staff worked day-to-day — not just what we assumed from the outside.

**Action:** I built the AWS + Supabase/Postgres stack with RLS, S3 media storage, CI/CD, and monitoring myself, then ran weekly forward-deployed sessions on-site with nurses and staff across all 4 pilot locations. I'd watch them use the product live, note friction points, and turn those observations directly into sprint work — closing the loop between clinical reality and code.

**Result:** Those sessions converted into 15+ shipped product changes, and the feedback loop became core to how we iterated at YC pace — grounded in 20+ design-partner interviews rather than guesswork.

**Lesson:** Owning infra and being the one in the room with users made me a much sharper prioritizer — I stopped building what was interesting and started building what was observably blocking adoption.

**Follow-ups:**
- *"What was a change you pushed back on because it didn't match the data model?"* → hint: tie to PHI/analytics separation (30+ tables) — explain a request that would've broken RLS boundaries.
- *"How did you decide what NOT to build with only 2 people?"* → hint: talk about triaging the 15+ changes — frequency across sites vs. one-off asks.

---

## Story 2: Hard Technical — Partial-Failure Handling in the Voice Pipeline

**Best for:** Systems design, distributed systems, backend/infra roles, "tell me about a hard technical problem"

**Situation:** At Freya (YC S25, real-time LLM voice agents), our voice pipeline chained together SIP ingress, STT, LLM, and TTS across 3 different vendor pairs. Any single vendor hiccup — a slow STT response, a dropped TTS stream — could kill a live call mid-conversation, which is unacceptable for a product where the "product" *is* the phone call.

**Task:** I needed to design retry and fallback orchestration that could isolate a vendor outage or transient failure without the caller ever noticing, and without double-processing any stage of the pipeline.

**Action:** I built orchestration logic that tracked call state per stage, so a failure in one STT/LLM/TTS pair could trigger a fallback to a secondary vendor mid-call rather than dropping the session. I made each retry idempotent — critical because retrying an LLM call or TTS synthesis without safeguards can produce duplicate audio or repeated turns. I also instrumented end-to-end latency tracing across the full pipeline to find where failures and tail latency actually originated.

**Result:** Call completion rate rose ~8pp, and p99 turn latency dropped ~35% by eliminating tail spikes tied to vendor stalls.

**Lesson:** Idempotency has to be designed at the stage boundary, not bolted on after — I learned to ask "what happens if this exact step runs twice" before writing the retry logic, not after.

**Follow-ups:**
- *"How did you actually implement idempotency — dedupe keys, sequence numbers?"* → hint: [CHECK] — source bullets don't specify a workflow engine (e.g., Temporal); confirm actual mechanism (idempotency keys per call-turn, state machine) before claiming any specific framework.
- *"How did you load-test this before it hit real customers?"* → hint: reference the 250+ concurrent SIP/WebRTC call harness and what broke first under load.

---

## Story 3: Feedback — When Nurses Told Me My Design Was Wrong

**Best for:** Receiving feedback, humility, user-centricity, "tell me about a time you were wrong"

**Situation:** At Framewise, I'd designed part of the patient app's video workflow assuming discharge nurses would want a lightweight, minimal-click interface — fewer steps, less friction, get in and out fast. It seemed obviously right from an engineering standpoint.

**Task:** During one of our weekly on-site sessions at a pilot hospital, a nurse flagged that the streamlined flow was actually causing errors — she wanted more explicit confirmation steps before certain stages of the 9-stage video workflow, especially anything touching patient-identifying info, because she was interruption-prone mid-shift and needed the UI to "hold her place."

**Action:** My instinct was to defend the design — fewer clicks felt like the right metric. But I sat with a few more nurses across sessions and realized the friction I was optimizing away was actually a safety feature for their real working conditions: constant interruptions, shared devices, high-stakes data. I reversed course and added explicit checkpoint confirmations at PHI-sensitive stages, even though it added steps.

**Result:** That change was one of the 15+ shipped from these sessions, and it became a talking point with other pilot sites — several independently asked for the same checkpoints.

**Lesson:** "Fewer clicks" is not a universal good — it's a proxy that only holds when you understand the user's actual failure modes, and I was wrong to assume mine without asking.

**Follow-ups:**
- *"Did any other assumption get overturned this way?"* → hint: mention the broader pattern across 20+ design-partner interviews — engineering intuition vs. clinical reality.
- *"How do you now validate a design assumption before building it?"* → hint: talk about earlier/more frequent nurse check-ins, lower-fidelity prototypes.

---

## Story 4: Failure — Winding Down Framewise

**Best for:** Failure/resilience questions, "tell me about a time something didn't work out," maturity under pressure

**Situation:** I co-founded Framewise in March 2026 as CTO, building healthcare infrastructure and running pilots across 4 sites with real patients and real clinical staff. By July 2026, after roughly four months, my co-founder and I made the decision to wind the company down.

**Task:** Beyond the technical and product work, I had to be honest with myself — and with our pilot partners and the small user base we'd onboarded — about whether the traction and timeline justified continuing, and I had to help close things out responsibly rather than let it fade.

**Action:** We looked hard at the pace of adoption relative to runway and the difficulty of scaling a PHI-sensitive product past a handful of design partners on a 2-person team. Rather than dragging it out, we made the call early enough to wind down cleanly — communicating with pilot sites, making sure no patient data was left in an unclear state, and documenting what we'd learned.

**Result:** The product itself didn't survive, but the infrastructure decisions (RLS-based PHI separation, forward-deployed feedback loops) and the discipline of shipping under YC-pace constraints directly shaped how I evaluate technical and product tradeoffs now.

**Lesson:** Speed of learning matters more than speed of building — I'd rather find out in month 4 that the market/timing isn't there than optimize infrastructure for a scale we were never going to reach.

**Follow-ups:**
- *"What would you have done differently starting over?"* → hint: earlier/harder go/no-go checkpoints tied to pilot conversion, not just feature velocity.
- *"How did you handle telling the pilot sites?"* → hint: emphasize responsible data handling and transparency — protects credibility for future ventures.
---

## Story 5: Systems — The Agent Factory (Personal Automation Fleet)

**Best for:** "side projects you're proud of", automation/AI-agent roles, reliability engineering, "what do you build for fun"

**Situation:** Outside of work I run a small fleet of always-on agents on my own hardware: a CRM enrichment worker that does LLM-based structured extraction over 1,700+ professional contacts, scheduled communication digests, and several task-specific workers, all supervised by launchd.

**Task:** The interesting problem wasn't any single agent — it was keeping a heterogeneous fleet healthy without babysitting it: services wedge, tokens expire, dependencies vanish, and a silent failure can go unnoticed for weeks.

**Action:** I built a self-healing layer: a supervisor sweeps every agent on a 30-minute cadence, checks each one's *actual* health signal (log freshness inside its work window, a reachable debug port, a fresh database write — not just "process exists"), applies one targeted remedy, and escalates only what it couldn't fix into a daily digest I can reply to. Replies route back into agent actions. Every automated decision writes an audit line so I can trace why anything happened.

**Result:** The supervisor caught a worker that had crash-looped ~49,000 times on a missing dependency within its first hour of operation, and OAuth-token failures now fail over automatically instead of silently killing email flows. The fleet runs for weeks without intervention.

**Lesson:** For autonomous systems, the health check has to observe the *outcome* (fresh data, reachable port), never the process table — and escalation paths matter more than remediation, because the failure you didn't anticipate is the only one that hurts.

**Follow-ups:**
- *"How do you avoid the supervisor itself being a single point of failure?"* → hint: it's stateless, idempotent, and its own absence shows up as staleness in the daily digest.
- *"What's the hardest bug the fleet surfaced?"* → hint: the 49k crash-loop — silent because the supervisor-less fleet had no freshness check on that worker's output.
