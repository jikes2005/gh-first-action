# Incident Response Runbook (Sample)

## Severity levels
- **SEV1** – Customer-facing outage or confirmed data breach. Page the on-call security lead immediately. Acknowledge within 15 minutes; executive notification within 1 hour.
- **SEV2** – Degraded service or suspected compromise of a single host. Acknowledge within 30 minutes.
- **SEV3** – Minor issue with no customer impact. Handle within the next business day.

## Escalation
1. On-call engineer triages and assigns a severity.
2. SEV1/SEV2: open an incident channel named `#inc-<date>-<short-name>`.
3. If not acknowledged within the SLA, escalate to the secondary on-call, then to the Head of Security.

## Containment
- Isolate affected EC2 instances by moving them to the quarantine security group.
- Rotate any credentials that may have been exposed (IAM access keys, database passwords, API tokens).
- Preserve evidence: snapshot EBS volumes before terminating anything.

## Post-incident
A blameless post-mortem is due within 5 business days for SEV1 and SEV2 incidents.
