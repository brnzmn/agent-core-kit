---
name: recover-background-work
description: Diagnose stalled or duplicated background jobs and implement safe recovery across worker ownership, retries, cancellation, and result persistence.
---

Trace an affected job from acquisition through its last durable effect. Distinguish queue delay, worker loss, slow progress, final-write failure, and stale display using observed state; a percentage does not identify the cause.

Inspect durable job ID, attempt/owner, heartbeat, last progress, cancellation, step receipts, and final result. Reuse the existing queue and state model. Heartbeats show liveness; progress shows advancement. A timeout alone does not prove the worker stopped.

Use atomic ownership acquisition and a generation/fencing token so an old worker cannot commit after replacement. Retry safe steps with the same operation identity. For an uncertain external write, reconcile by idempotency key or effect receipt before replaying; pause that effect if duplication cannot be resolved safely, while continuing independent recovery.

Persist completion only when the required result is durable. Model cancellation and failure explicitly, including who can resume and how partial output is treated. Set retry/backoff and resource limits from the queue contract and failure type; do not invent a task-wide attempt limit.

Poll small status/version responses with backoff or change cursors; fetch large results when ready. Use existing automation ownership when scheduling is requested; do not create an additional producer to compensate for a stuck job.

Verify applicable interruption, duplicate delivery, stale-owner commit, cancellation, and final-write failure paths. Bind the observed result and coverage gaps to this run and revision; a scheduler's success is not proof of the intended effect.
