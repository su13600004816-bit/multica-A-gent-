# PL-140 blocked owner evidence

This directory carries the evidence extracted from `agent/claude/404b04f1` for the
PL-140 blocked-owner handoff.

Scope of this PR:

- Keep the blocked-owner acceptance criteria in `BLOCKED_OWNER_ACCEPTANCE.md`.
- Keep the captured probe output in `probes/blocked_evidence.txt`.
- Keep the reproduction probe script in `probes/probe_blocked_credentials.sh`.

Out of scope for this PR:

- X20 platform identity hard-reject implementation.
- X51 GitHub credential or verifier implementation.
- X63 systemd timer installation.
- X72 external notification and ACK callback implementation.

Those items still require platform, total-manager, or ops action as described in
the acceptance document.
