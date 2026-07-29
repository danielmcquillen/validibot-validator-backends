# Security Policy

## Supported Versions

| Release | Supported |
| --- | --- |
| Latest published version of each backend | :white_check_mark: |
| Older backend releases | :x: |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
responsibly. **Do not open a public GitHub issue for security vulnerabilities.**

Use this repository's **Security** tab to submit a private vulnerability
report. If that is unavailable, email
[security@validibot.com](mailto:security@validibot.com).

Please include:

- A description of the vulnerability
- Steps to reproduce the issue
- The potential impact
- Any suggested fixes (if applicable)

We will acknowledge receipt within 3 business days and aim to provide an
initial assessment within 10 business days. We may ask for additional
information or guidance.

## Security Considerations

Validator containers execute user-supplied files (IDF building models, FMU
binaries, etc.) in isolated Docker environments. Deployments should follow
these practices:

- **Container isolation:** Run containers with `--network none`, read-only
  filesystems, memory limits, and CPU limits. The justfile's `deploy` command
  configures these for Cloud Run Jobs.
- **Non-root execution:** Containers run as a non-root `validibot` user
  (UID 1000).
- **Image provenance:** Resolve a version tag to its immutable digest, verify
  the GitHub artifact attestation, and deploy by digest rather than `:latest`.
- **Access control:** Restrict who can push container images. Use separate
  service accounts for build/push vs. runtime execution.
- **Input validation:** The Validibot platform validates input envelopes
  before dispatching to containers, but validators should not be exposed
  directly to untrusted input without the platform's envelope validation.

## Scope

This security policy covers the `validibot-validator-backends` repository only. For
security issues in other Validibot components, see:

- [validibot](https://github.com/mcquilleninteractive/validibot) (core platform)
- [validibot-shared](https://github.com/mcquilleninteractive/validibot-shared) (shared models)
