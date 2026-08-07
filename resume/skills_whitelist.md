# Claimable Skills Whitelist
#
# Skills David can TRUTHFULLY claim beyond what the base resume lists, each with
# provenance he can defend in an interview. The tailor may add these to the
# Skills/Coursework lines ONLY when the job description calls for them.
# Anything a JD wants that is neither on the base resume nor here (e.g. QNX,
# AUTOSAR, CANoe, MISRA, Kubernetes, Rust) must NOT be claimed.
#
# Format: Skill — provenance (why it's defensible)

## Systems / embedded (backed by verified Brown coursework, see courses.md)
- C — Weenix Unix-like kernel (CSCI 1670/1690), CSCI 1600 embedded projects,
  CSCI 0300/1310 systems coursework
- Kernel development — built a Unix-like kernel (processes, VFS, VM) in the
  Weenix lab of Brown's OS sequence
- RTOS fundamentals — CSCI 1600 Real-Time & Embedded Software (scheduling,
  deadlines, interrupt-driven design). Fundamentals only: do NOT claim
  production FreeRTOS/QNX/Zephyr experience.
- Verilog/FPGA — ENGN 1630 Digital Electronics Systems Design
- Computer architecture — ENGN 1640 Design of Computing Systems
- Concurrency/synchronization — CSCI 1760 Multiprocessor Synchronization
- Networking (TCP/IP) — CSCI 1680 Computer Networks
- Circuits & signals — ENGN 0520, ENGN 1570

## Infra / tooling (backed by work experience)
- Linux — daily driver for all infra work: Docker images, AWS deployments,
  CI/CD runners, server debugging across Framewise/Freya/personal projects
- Bash/shell scripting — CI/CD pipelines, launchd/cron automation, deploy scripts
- Git — used everywhere (already on base resume, listed for completeness)
- REST APIs — Freya distributed backend services, Framewise apps
- PostgreSQL — Supabase Postgres with RLS at Framewise (base resume says
  "Supabase"; "PostgreSQL"/"Postgres" is the same claim in JD vocabulary)
- WebRTC/SIP — Freya load-testing harness and voice pipeline work

## In progress (claim as "(in progress)" once repos are public)
- CAN/CAN-FD, UDS (ISO 14229), DBC tooling — zonal-ecu-sim project being built
  now at ~/zonal-ecu-sim; claimable once pushed to GitHub with real commits

## Explicitly NOT claimable (recorded decisions)
- gRPC, QNX, AUTOSAR, MISRA, CANoe/Vector tools, dSPACE, ISO 26262, Yocto,
  Zephyr, Rust, Kubernetes, Lauterbach, HSM/SHE
