# Changelog

Kimi's user-facing release notes for server owners, staff, and members.

## Unreleased

### New

- Experimental owner-approved configuration control plane. Trusted modules can
  propose configuration changes; the bot owner reviews them with
  `/proposals list`, `/proposals show`, `/proposals approve`,
  `/proposals reject`, and stages secrets with `/proposals stage-secret`.
  Off by default (`CONTROL_PLANE_ENABLED`).
- Application modules install through a versioned public module API and can
  declare the control-plane capabilities they need.

## 1.0.0 (2026-08-24)

First public release.
