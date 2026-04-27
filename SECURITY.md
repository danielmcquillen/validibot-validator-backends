# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
responsibly. **Do not open a public GitHub issue for security vulnerabilities.**

**Email:** [security@validibot.com](mailto:security@validibot.com)

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
- **Image provenance:** Use immutable image tags (git SHA) rather than
  `:latest` in production. Only pull images from your own private registry.
- **Access control:** Restrict who can push container images. Use separate
  service accounts for build/push vs. runtime execution.
- **Input validation:** The Validibot platform validates input envelopes
  before dispatching to containers, but validators should not be exposed
  directly to untrusted input without the platform's envelope validation.

## Scope

This security policy covers the `validibot-validator-backends` repository only. For
security issues in other Validibot components, see:

- [validibot](https://github.com/danielmcquillen/validibot) (core platform)
- [validibot-shared](https://github.com/danielmcquillen/validibot-shared) (shared models)
