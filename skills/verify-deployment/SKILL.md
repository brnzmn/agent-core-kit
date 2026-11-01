---
name: verify-deployment
description: Diagnose deployment-only failures and verify that the intended domain runs the expected revision, runtime dependencies, and changed user flow.
---

Resolve repository, provider account/project, environment, domain, and expected revision from current configuration and live deployment metadata. Missing local links do not establish a need for a new provider project. Reuse available provider tools or skills; confirm current commands from authoritative documentation when needed.

Separate build success, packaged artifacts, startup, routing, and application behavior. Inspect the actual runtime artifact when binaries, generated files, environment variables, or native dependencies are involved. Existence in the source tree or build machine does not establish availability in the deployed function/container.

For reachability or latency, compare DNS/TLS, redirect chain, static response, and dynamic route as relevant. State causes as hypotheses until the measured layer supports them. Preserve security gates and configuration when reconciling stale code or branches.

Carry out fixes and deployments within the user's existing scope and authorization. Before a mutation, resolve any ambiguity that could target the wrong account or production environment. Do not silently create infrastructure, change domains, or select another project to get a green deployment.

Read the deployed revision back and exercise the changed user action through the intended URL. Recheck affected authorization or persistence boundaries. Record deployment ID/revision, runtime, action, and observed result; label local-only or unavailable checks. Use a verified recovery target for an authorized rollback, and verify its effect too.
