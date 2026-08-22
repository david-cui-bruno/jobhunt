# AWS Discovery and Tailoring Offload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional AWS preprocessing plane for public-source discovery and de-identified tailoring manifests while keeping SQLite, final PDF rendering, secrets, and browser submission on the Mac.

**Architecture:** Versioned JSON contracts cross three SQS queues. Lambda polls public sources and publishes normalized candidates. Fargate workers consume sanitized tailoring jobs and write encrypted manifests to S3. A Mac bridge uses short-lived AWS profile credentials, validates every payload, writes SQLite idempotently, renders resumes locally, and falls back to existing local work when AWS is unavailable.

**Tech Stack:** Python 3.9, dataclasses, JSON Schema-style validation, boto3, AWS SQS, Lambda, EventBridge Scheduler, ECS Fargate, ECR, S3, KMS, CloudWatch, Terraform, SQLite

**Spec:** `docs/superpowers/specs/2026-08-22-hybrid-pipeline-ashby-recovery-design.md`

## Global Constraints

- Complete the local dispatcher plan before enabling cloud mode.
- Cloud mode is disabled by default and all-local behavior remains available.
- AWS never performs final browser submission or writes directly to `tracker.db`.
- Cloud payloads exclude name, email, phone, address, OAuth material, Workday credentials, answer-bank values, browser profiles, screenshots, and final resume files.
- The first worker uses deterministic tailoring logic. Bedrock remains behind a disabled interface until model access and output parity are separately verified.
- SQS consumers are idempotent and safe under duplicate delivery.
- S3 uses SSE-KMS, public access blocking, versioning, and 14-day lifecycle deletion for normal artifacts.
- No NAT Gateway is introduced.
- No billable AWS resource is created without a separate explicit approval for `terraform apply`.
- Do not stage or commit `out/tracker.db`, AWS credentials, Terraform state, generated certificates, or final resume artifacts.

---

### Task 1: Define versioned contracts and a prohibited-PII gate

**Files:**
- Create: `cloud/__init__.py`
- Create: `cloud/contracts.py`
- Create: `cloud/privacy.py`
- Create: `tests/test_cloud_contracts.py`

**Interfaces:**
- Produces: `DiscoveryCandidate.from_dict(data: dict) -> DiscoveryCandidate`
- Produces: `TailorJob.from_dict(data: dict) -> TailorJob`
- Produces: `TailorManifest.from_dict(data: dict) -> TailorManifest`
- Produces: `to_json(message) -> str`
- Produces: `assert_cloud_safe(payload: dict) -> None`
- Produces: constants `SCHEMA_VERSION = 1` and `POLICY_REVISION = "hybrid-v1"`

- [ ] **Step 1: Write failing contract and privacy tests**

```python
# tests/test_cloud_contracts.py
import pytest

from cloud.contracts import DiscoveryCandidate, TailorJob, TailorManifest, to_json
from cloud.privacy import ProhibitedCloudData, assert_cloud_safe


def test_discovery_candidate_round_trips() -> None:
    item = DiscoveryCandidate.from_dict({
        "schema_version": 1,
        "source": "abc",
        "source_posting_id": "abc-1",
        "canonical_url": "https://boards.greenhouse.io/acme/jobs/1",
        "company": "Acme",
        "title": "Backend Engineer",
        "locations": ["New York"],
        "first_seen": 1_787_000_000,
        "raw_source_hash": "a" * 64,
    })
    assert '"schema_version":1' in to_json(item)


def test_tailor_job_rejects_contact_data_at_any_depth() -> None:
    payload = {
        "schema_version": 1,
        "idempotency_key": "job-1",
        "posting_id": "p1",
        "company": "Acme",
        "title": "Engineer",
        "sanitized_jd": "Build systems",
        "role_track": "full-time",
        "bullet_bank": [{"id": "b1", "text": "Built a service", "email": "hidden@example.com"}],
        "skills_whitelist": ["Python"],
        "source_revision": "abc-v1",
        "policy_revision": "hybrid-v1",
    }
    with pytest.raises(ProhibitedCloudData):
        assert_cloud_safe(payload)


def test_manifest_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        TailorManifest.from_dict({"schema_version": 2})
```

Add tests for phone-like values, OAuth keys, password keys, cookie keys, resume PDF suffixes, screenshots, unknown fields, missing fields, and canonical JSON ordering.

- [ ] **Step 2: Run tests and confirm modules are missing**

Run: `python3 -m pytest -q tests/test_cloud_contracts.py`

Expected: FAIL because the `cloud` package does not exist.

- [ ] **Step 3: Implement frozen dataclasses with strict parsing**

```python
# cloud/contracts.py
from dataclasses import asdict, dataclass
import json

SCHEMA_VERSION = 1
POLICY_REVISION = "hybrid-v1"

@dataclass(frozen=True)
class DiscoveryCandidate:
    schema_version: int
    source: str
    source_posting_id: str
    canonical_url: str
    company: str
    title: str
    locations: tuple[str, ...]
    first_seen: int
    raw_source_hash: str

    @classmethod
    def from_dict(cls, data: dict) -> "DiscoveryCandidate":
        require_exact_keys(data, cls)
        require_version(data)
        return cls(**{**data, "locations": tuple(data["locations"])})
```

Implement equivalent strict dataclasses for:

```python
TailorJob(schema_version, idempotency_key, posting_id, company, title,
          sanitized_jd, role_track, bullet_bank, skills_whitelist,
          source_revision, policy_revision)

TailorManifest(schema_version, idempotency_key, posting_id, role_type,
               coursework, selected_bullet_ids, ordered_skills,
               evidence, policy_revision, content_hash)
```

`to_json` must use `sort_keys=True` and compact separators.

- [ ] **Step 4: Implement recursive privacy validation**

Reject prohibited key fragments: `email`, `phone`, `address`, `oauth`, `token`, `password`, `cookie`, `credential`, `workday_account`, `screenshot`, `resume_pdf`, and `browser_profile`. Reject strings matching email addresses or North American phone-number patterns. Permit company names, title, public job URL, sanitized JD, approved bullet text, and skills.

- [ ] **Step 5: Run focused tests**

Run: `python3 -m pytest -q tests/test_cloud_contracts.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cloud/__init__.py cloud/contracts.py cloud/privacy.py tests/test_cloud_contracts.py
git commit -m "Define cloud preprocessing contracts"
```

### Task 2: Add idempotent cloud job state and a transport-neutral Mac bridge

**Files:**
- Create: `cloud/state.py`
- Create: `cloud/transport.py`
- Create: `cloud/bridge.py`
- Create: `tests/test_cloud_bridge.py`
- Modify: `submission/database.py`

**Interfaces:**
- Produces protocol: `CloudTransport.send(queue: str, body: str, *, dedupe_key: str) -> str`
- Produces protocol: `CloudTransport.receive(queue: str, *, limit: int, wait_seconds: int) -> list[ReceivedMessage]`
- Produces protocol: `CloudTransport.ack(message: ReceivedMessage) -> None`
- Produces: `ensure_cloud_state(conn) -> None`
- Produces: `record_job(conn, *, idempotency_key: str, posting_id: str, kind: str, payload_hash: str) -> bool`
- Produces: `record_result_once(conn, *, idempotency_key: str, result_hash: str) -> bool`
- Produces: `CloudBridge.ingest_discovery(messages) -> BridgeStats`
- Produces: `CloudBridge.ingest_tailor_results(messages) -> BridgeStats`

- [ ] **Step 1: Write failing idempotency tests with an in-memory transport**

```python
# tests/test_cloud_bridge.py

def test_duplicate_discovery_message_inserts_one_posting(cloud_db) -> None:
    body = discovery_json(source_posting_id="one")
    transport = MemoryTransport(messages=[body, body])
    bridge = CloudBridge(cloud_db, transport)
    stats = bridge.poll_discovery_once()
    assert stats.inserted == 1
    assert stats.duplicates == 1
    assert cloud_db.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    assert transport.acked == 2


def test_result_is_not_acked_when_local_commit_fails(cloud_db, monkeypatch) -> None:
    transport = MemoryTransport(messages=[tailor_manifest_json()])
    bridge = CloudBridge(cloud_db, transport)
    monkeypatch.setattr(bridge, "apply_manifest", lambda manifest: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(sqlite3.OperationalError):
        bridge.poll_tailor_results_once()
    assert transport.acked == 0
```

Add tests for payload-hash mismatch, privacy-gate rejection, duplicate tailor result, and an AWS-unavailable path returning to local preprocessing.

- [ ] **Step 2: Run tests and confirm bridge modules are missing**

Run: `python3 -m pytest -q tests/test_cloud_bridge.py`

Expected: FAIL because state and bridge modules do not exist.

- [ ] **Step 3: Implement cloud job schema**

```sql
CREATE TABLE IF NOT EXISTS cloud_jobs (
    idempotency_key TEXT PRIMARY KEY,
    posting_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_hash TEXT,
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    last_error TEXT
);
```

Insert with `ON CONFLICT DO NOTHING`; return whether a new row was created. A result can transition `sent` to `completed` once when hashes match.

- [ ] **Step 4: Implement the transport protocol and memory transport**

Use dataclasses and `typing.Protocol`. Keep boto3 out of this module so all bridge behavior is testable offline.

- [ ] **Step 5: Implement discovery ingestion**

Validate contract and privacy, derive the existing posting ID using the same source-specific logic as local watchers, run filters, and write SQLite in one short transaction. Acknowledge only after commit or recognized duplicate.

- [ ] **Step 6: Implement tailoring result ingestion seam**

`apply_manifest` validates the content hash and calls a renderer interface. Do not compile inside the transport loop while holding a database transaction. Claim the cloud job, render locally, then compare-and-set the posting to `ready` after quality checks.

- [ ] **Step 7: Run bridge and database tests**

Run: `python3 -m pytest -q tests/test_cloud_bridge.py tests/test_submission_attempts.py test_throughput.py`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add cloud/state.py cloud/transport.py cloud/bridge.py tests/test_cloud_bridge.py \
  submission/database.py
git commit -m "Add idempotent Mac cloud bridge"
```

### Task 3: Build public-source Lambda discovery workers

**Files:**
- Create: `cloud/workers/__init__.py`
- Create: `cloud/workers/discovery.py`
- Create: `cloud/workers/source_registry.py`
- Create: `tests/test_cloud_discovery.py`
- Modify: `watcher/abc_startups.py`
- Modify: `watcher/bigco.py`

**Interfaces:**
- Produces: `SourcePoller.poll() -> list[DiscoveryCandidate]`
- Produces: `handler(event: dict, context) -> dict`
- Produces: `source_registry() -> dict[str, SourcePoller]`
- Consumes: public JSON, RSS, and documented ATS board endpoints only

- [ ] **Step 1: Write failing source and handler tests**

```python
# tests/test_cloud_discovery.py

def test_handler_publishes_normalized_candidates(monkeypatch) -> None:
    monkeypatch.setattr("cloud.workers.discovery.source_registry", lambda: {
        "bigco": FakePoller([candidate("p1"), candidate("p1")])
    })
    sent = []
    monkeypatch.setattr("cloud.workers.discovery.send_candidate", lambda item: sent.append(item))
    result = handler({"source": "bigco"}, None)
    assert result == {"source": "bigco", "found": 2, "published": 1}
    assert sent[0].source_posting_id == "p1"


def test_registry_contains_no_browser_source() -> None:
    for poller in source_registry().values():
        assert poller.requires_browser is False
```

Add tests that network exceptions produce a failed invocation without partial duplicate publication and that every candidate passes `assert_cloud_safe`.

- [ ] **Step 2: Run tests and confirm worker modules are missing**

Run: `python3 -m pytest -q tests/test_cloud_discovery.py`

Expected: FAIL because cloud discovery workers do not exist.

- [ ] **Step 3: Extract pure poll functions from existing watchers**

Refactor `abc_startups.py` and `bigco.py` so public endpoint polling returns normalized Python dictionaries without opening SQLite. Keep the existing local `run()` methods as adapters that call the pure functions and write locally.

- [ ] **Step 4: Implement the source registry and Lambda handler**

The handler receives one source name, polls it, deduplicates within the invocation by `(source, source_posting_id)`, validates privacy, and publishes compact JSON to the discovery-result queue. It returns counts and raises after any unrecoverable source error so CloudWatch records the failure.

- [ ] **Step 5: Run watcher parity tests**

Run: `python3 -m pytest -q tests/test_cloud_discovery.py tests/test_abc_startups.py tests/test_bigco.py test_throughput.py`

Expected: PASS with identical local watcher results for shared fixtures.

- [ ] **Step 6: Commit**

```bash
git add cloud/workers/__init__.py cloud/workers/discovery.py \
  cloud/workers/source_registry.py tests/test_cloud_discovery.py \
  watcher/abc_startups.py watcher/bigco.py
git commit -m "Extract cloud-safe discovery workers"
```

### Task 4: Produce deterministic tailoring manifests and render them locally

**Files:**
- Create: `cloud/tailoring.py`
- Create: `cloud/workers/tailor.py`
- Create: `tests/test_cloud_tailoring.py`
- Modify: `tailor/tailor.py:248-339,390-415,578-691`
- Create: `deploy/aws/offload/Dockerfile.tailor`
- Create: `deploy/aws/offload/requirements.txt`

**Interfaces:**
- Produces: `build_tailor_job(posting_id: str, company: str, title: str, jd: str) -> TailorJob`
- Produces: `plan_manifest(job: TailorJob) -> TailorManifest`
- Produces: `render_manifest(manifest: TailorManifest, *, jd: str) -> Path | None`
- Produces: `cloud.workers.tailor.main() -> int`
- Consumes: deterministic role, coursework, skills, and quality rules from `tailor/tailor.py`

- [ ] **Step 1: Write failing local-cloud parity tests**

```python
# tests/test_cloud_tailoring.py

def test_manifest_contains_only_allowlisted_resume_choices() -> None:
    job = build_tailor_job("p1", "Acme", "Backend Engineer", "Python distributed systems")
    assert_cloud_safe(asdict(job))
    manifest = plan_manifest(job)
    assert manifest.role_type == "backend"
    assert set(manifest.ordered_skills) <= set(job.skills_whitelist)
    assert {item for item in manifest.selected_bullet_ids} <= {b["id"] for b in job.bullet_bank}


def test_local_manifest_render_matches_current_deterministic_resume(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tailor, "OUT_DIR", tmp_path)
    jd = "Python distributed systems"
    direct = tailor.build_grounded_resume("Backend Engineer", jd)
    manifest = plan_manifest(build_tailor_job("p1", "Acme", "Backend Engineer", jd))
    rendered = render_manifest_source(manifest, jd=jd)
    assert rendered == direct
```

Add tests that the manifest content hash covers all fields, unknown bullet IDs fail closed, and no final resume or contact field enters a job.

- [ ] **Step 2: Run tests and confirm tailoring interfaces are missing**

Run: `python3 -m pytest -q tests/test_cloud_tailoring.py`

Expected: FAIL because cloud tailoring modules do not exist.

- [ ] **Step 3: Extract deterministic selection data from `tailor.py`**

Expose pure functions for course selection, role classification, and JD skill ordering. Assign stable IDs to approved bullet-bank entries. Do not change the current `tailor()` output.

- [ ] **Step 4: Implement the deterministic planner and local renderer**

The first `plan_manifest` uses no model call. It selects only reviewed IDs and allowlisted skills, records evidence, and hashes canonical JSON. `render_manifest_source` applies the manifest to `BASE_TEX`, adds local contact details from the base file, and runs the existing sanitize, validation, one-page compile, and quality metadata path.

- [ ] **Step 5: Implement the Fargate worker entrypoint**

The worker receives one SQS message reference from environment or command argument, loads the `TailorJob`, validates privacy, writes the manifest JSON to `s3://$ARTIFACT_BUCKET/manifests/<idempotency_key>.json` with KMS encryption, publishes a `TailorManifest` result message, and deletes the source SQS message only after both writes succeed.

The image installs Python dependencies only. It does not install Chrome, Playwright, Gmail libraries, or LaTeX because it does not submit or render final PDFs.

- [ ] **Step 6: Add a disabled Bedrock planner interface without enabling it**

Define:

```python
class Planner(Protocol):
    def plan(self, job: TailorJob) -> TailorManifest: ...

class DeterministicPlanner:
    def plan(self, job: TailorJob) -> TailorManifest:
        return plan_manifest(job)
```

Reject `JOBHUNT_CLOUD_PLANNER=bedrock` with a clear configuration error until a later approved parity task supplies `BedrockPlanner`. This prevents accidental reintroduction of unreviewed model rewrites.

- [ ] **Step 7: Run parity and resume quality tests**

Run: `python3 -m pytest -q tests/test_cloud_tailoring.py test_resume_quality.py tests/test_resume_awards.py tests/test_resume_framewise_copy.py tests/test_resume_visual_consistency.py`

Expected: PASS; source output matches the local deterministic path.

- [ ] **Step 8: Commit**

```bash
git add cloud/tailoring.py cloud/workers/tailor.py tests/test_cloud_tailoring.py \
  tailor/tailor.py deploy/aws/offload/Dockerfile.tailor \
  deploy/aws/offload/requirements.txt
git commit -m "Add de-identified tailoring manifests"
```

### Task 5: Add the boto3 transport and cloud-disabled local fallback

**Files:**
- Create: `cloud/aws_transport.py`
- Create: `cloud/runtime.py`
- Create: `cloud_bridge.py`
- Create: `tests/test_cloud_runtime.py`
- Modify: `drip.py`
- Modify: `launchd/com.jobhunt.drip.plist`
- Modify: `deploy/jobhunt.env.example`

**Interfaces:**
- Produces: `AwsTransport.from_profile(profile: str, region: str, queue_urls: dict[str, str]) -> AwsTransport`
- Produces: `cloud_enabled() -> bool`
- Produces: `tailor_or_fallback(posting, *, transport=None) -> Path | None`
- Produces CLI: `python3 cloud_bridge.py --once --dry-run`

- [ ] **Step 1: Write failing fallback and credential tests**

```python
# tests/test_cloud_runtime.py

def test_cloud_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("JOBHUNT_CLOUD_ENABLED", raising=False)
    assert cloud_enabled() is False


def test_missing_sso_credentials_falls_back_locally(monkeypatch) -> None:
    monkeypatch.setenv("JOBHUNT_CLOUD_ENABLED", "1")
    monkeypatch.setattr("cloud.runtime.AwsTransport.from_profile", mock.Mock(side_effect=NoCredentialsError()))
    local = mock.Mock(return_value=Path("resume.pdf"))
    assert tailor_or_fallback(fake_posting(), local_tailor=local) == Path("resume.pdf")
    local.assert_called_once()
```

Add tests for 20-second SQS long polling, message acknowledgement, visibility extension, KMS encryption headers, and no credential value in logs.

- [ ] **Step 2: Run tests and confirm runtime modules are missing**

Run: `python3 -m pytest -q tests/test_cloud_runtime.py`

Expected: FAIL because runtime and AWS transport modules do not exist.

- [ ] **Step 3: Implement boto3 transport behind the protocol**

Construct `boto3.Session(profile_name=profile, region_name=region)`. Use queue URLs from environment. Set `WaitTimeSeconds=20`, bounded batch sizes, and explicit visibility timeout. S3 writes must set `ServerSideEncryption='aws:kms'` and the configured KMS key ID.

- [ ] **Step 4: Implement safe feature flags and fallback**

Cloud use requires `JOBHUNT_CLOUD_ENABLED=1`, queue URLs, bucket, KMS key, region, and profile. Missing or expired credentials log one redacted line and use the local path. Never store access keys or SSO cache content in launchd plists.

- [ ] **Step 5: Add the bridge CLI and drip integration**

`cloud_bridge.py --once --dry-run` validates connectivity and prints counts without writing SQLite or deleting messages. Normal bridge mode ingests discovery and tailoring results. Drip publishes a tailoring job only when cloud is enabled, otherwise it calls local `tailor()` exactly as before.

- [ ] **Step 6: Run runtime and local regression tests**

Run: `python3 -m pytest -q tests/test_cloud_runtime.py tests/test_cloud_bridge.py test_throughput.py`

Expected: PASS with no AWS credentials required.

- [ ] **Step 7: Commit**

```bash
git add cloud/aws_transport.py cloud/runtime.py cloud_bridge.py \
  tests/test_cloud_runtime.py drip.py launchd/com.jobhunt.drip.plist \
  deploy/jobhunt.env.example
git commit -m "Add optional AWS preprocessing runtime"
```

### Task 6: Define isolated Terraform for queues, KMS, S3, Lambda, and Fargate

**Files:**
- Create: `deploy/aws/offload/versions.tf`
- Create: `deploy/aws/offload/variables.tf`
- Create: `deploy/aws/offload/main.tf`
- Create: `deploy/aws/offload/queues.tf`
- Create: `deploy/aws/offload/storage.tf`
- Create: `deploy/aws/offload/compute.tf`
- Create: `deploy/aws/offload/iam.tf`
- Create: `deploy/aws/offload/monitoring.tf`
- Create: `deploy/aws/offload/outputs.tf`
- Create: `deploy/aws/offload/terraform.tfvars.example`
- Create: `deploy/aws/offload/README.md`
- Create: `tests/test_aws_offload.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces Terraform outputs: queue URLs, artifact bucket, KMS key ARN, Lambda names, ECS cluster ARN, task definition ARN
- Consumes: discovery Lambda zip and tailoring worker ECR image URI

- [ ] **Step 1: Write failing static infrastructure safety tests**

```python
# tests/test_aws_offload.py

def test_offload_module_has_no_nat_gateway_or_browser_compute() -> None:
    text = read_all_tf()
    assert "aws_nat_gateway" not in text
    assert "playwright" not in text.lower()
    assert "chromium" not in text.lower()


def test_every_queue_has_dlq_and_kms() -> None:
    text = (OFFLOAD / "queues.tf").read_text()
    for name in ("discovery_results", "tailor_jobs", "tailor_results"):
        block = hcl_block(text, f'resource "aws_sqs_queue" "{name}"')
        assert "redrive_policy" in block
        assert "kms_master_key_id" in block


def test_artifact_bucket_is_private_kms_encrypted_and_expires_manifests() -> None:
    text = (OFFLOAD / "storage.tf").read_text()
    assert "aws_s3_bucket_public_access_block" in text
    assert 'sse_algorithm     = "aws:kms"' in text
    assert "expiration" in text
    assert "days = 14" in text
```

Add tests for no final-resume prefix, least-privilege worker IAM, Lambda reserved concurrency, Fargate desired count ceiling of 5, queue-age alarms, DLQ alarms, budget tags, and `prevent_destroy` on the KMS key and artifact bucket.

- [ ] **Step 2: Run tests and confirm the module is missing**

Run: `python3 -m pytest -q tests/test_aws_offload.py`

Expected: FAIL because `deploy/aws/offload` Terraform files do not exist.

- [ ] **Step 3: Implement the isolated Terraform root module**

Do not reference or apply the existing EC2 module. Use its naming and tagging conventions only. Define three encrypted standard SQS queues with DLQs, one KMS key, one private versioned S3 bucket, one ECR repository, one Lambda role and function, one ECS cluster, one Fargate task definition, and one ECS service that long-polls the tailoring queue. Set `desired_count` from `var.tailor_desired_count` with default `0`; application autoscaling may raise it from 0 to 5 only after cloud mode is explicitly enabled. EventBridge schedules invoke only approved public-source Lambda pollers.

- [ ] **Step 4: Implement least-privilege IAM**

Lambda may send only to the discovery-results queue and write logs. Tailor tasks may receive and delete only from tailor-jobs, send only to tailor-results, read and write only `manifests/*`, use only the offload KMS key, and write logs. No role receives access to EC2, Gmail, SSM answer parameters, browser state, or the existing backup prefixes.

- [ ] **Step 5: Implement monitoring and hard limits**

Add CloudWatch alarms for oldest-message age and DLQ depth. Set Lambda reserved concurrency and Fargate scaling maximum to 5. Tag every resource with project, environment, component, and managed-by values.

- [ ] **Step 6: Add outputs and a no-apply README**

Document SSO login, image build, Lambda packaging, `terraform init`, `fmt`, `validate`, and `plan`. Put `terraform apply` in a separately labeled section that states it requires explicit approval and creates billable resources.

- [ ] **Step 7: Run static tests and Terraform validation**

Run: `python3 -m pytest -q tests/test_aws_offload.py test_aws_deploy.py`

If Terraform is missing, install the official CLI through Homebrew, then run:

```bash
terraform -chdir=deploy/aws/offload fmt -check
terraform -chdir=deploy/aws/offload init -backend=false
terraform -chdir=deploy/aws/offload validate
terraform -chdir=deploy/aws/offload plan -refresh=false -out="$JCODE_SCRATCH_DIR/job-hunt-offload.tfplan"
```

Do not run `terraform apply`.

- [ ] **Step 8: Commit**

```bash
git add deploy/aws/offload tests/test_aws_offload.py .gitignore
git commit -m "Define AWS preprocessing infrastructure"
```

### Task 7: Verify end-to-end behavior without creating AWS resources

**Files:**
- Create: `scripts/smoke_cloud_offload.py`
- Modify: `README.md`
- Modify: `docs/system-design.html`
- Modify: `deploy/aws/offload/README.md`

**Interfaces:**
- Produces CLI: `python3 scripts/smoke_cloud_offload.py --memory-transport`
- Consumes: all cloud contracts, bridge, workers, deterministic manifest, and local renderer

- [ ] **Step 1: Implement an offline end-to-end smoke harness**

The harness uses `MemoryTransport` and a temporary SQLite database to execute:

1. fake public candidate publication
2. bridge discovery ingestion
3. TailorJob publication
4. deterministic worker manifest generation
5. duplicate TailorManifest delivery
6. one local PDF render and quality gate
7. ready-state transition

It asserts one posting row, one cloud job result, one final PDF, and no duplicate state transition.

- [ ] **Step 2: Add a payload dump and privacy assertion**

Write captured JSON under `$JCODE_SCRATCH_DIR`, run `assert_cloud_safe` on each payload, scan for contact data from `profile/profile.yaml`, and fail if any prohibited value appears. Delete the scratch payloads after the report.

- [ ] **Step 3: Run the offline acceptance suite**

```bash
python3 scripts/smoke_cloud_offload.py --memory-transport
python3 -m pytest -q
python3 cloud_bridge.py --once --dry-run
terraform -chdir=deploy/aws/offload fmt -check
terraform -chdir=deploy/aws/offload validate
```

Expected:

- memory-transport smoke passes
- full suite passes
- cloud bridge dry-run either validates short-lived credentials or cleanly reports local fallback
- Terraform validates
- no AWS resource is created

- [ ] **Step 4: Update system documentation**

Document trust boundaries, message flow, local fallback, credential behavior, operational metrics, and exact commands for disabling cloud mode. State that the active final submit path remains on the Mac.

- [ ] **Step 5: Request the separate infrastructure creation approval**

Present the reviewed Terraform plan summary, expected monthly cost before credits, services and regions, resource count, concurrency ceilings, and rollback steps. Wait for explicit approval before any `terraform apply`.

- [ ] **Step 6: Commit**

```bash
git add scripts/smoke_cloud_offload.py README.md docs/system-design.html \
  deploy/aws/offload/README.md
git commit -m "Document and verify cloud offload workflow"
```

## Plan 3 Completion Gate

Before requesting infrastructure creation approval, verify:

```bash
python3 scripts/smoke_cloud_offload.py --memory-transport
python3 -m pytest -q
python3 cloud_bridge.py --once --dry-run
terraform -chdir=deploy/aws/offload fmt -check
terraform -chdir=deploy/aws/offload validate
git diff --check
git status --short
```

Required observations:

- All-local mode behaves exactly as before when `JOBHUNT_CLOUD_ENABLED` is unset.
- Duplicate messages do not duplicate postings, cloud jobs, PDFs, or state transitions.
- Captured cloud payloads contain no prohibited PII or secrets.
- Final PDF compilation and quality checks occur only on the Mac.
- Infrastructure contains no browser worker, NAT Gateway, inbound security rule, or access to local secrets.
- No Terraform apply has run.
- Terraform state and live database files are not staged.
