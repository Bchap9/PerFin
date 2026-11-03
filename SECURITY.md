# Security policy

PerFin handles some of the most sensitive data a person has. Security reports are
taken seriously and handled privately.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem. Instead use
GitHub's private vulnerability reporting on this repository ("Report a
vulnerability" under the Security tab). Include:

- A description of the issue and its impact
- Steps to reproduce, or a proof of concept
- The version or commit you tested against

You will get an acknowledgement within 72 hours and a status update at least
every 7 days until the issue is resolved.

## Scope

In scope: anything in this repository, the published container images, and the
default Docker Compose deployment.

Out of scope: vulnerabilities in third-party services you connect (your IdP, your
mail provider, a price API) unless PerFin's integration with them is at fault.

## Supported versions

Until 1.0, only the latest release receives security fixes.

## Hardening guidance

See [docs/SECURITY.md](docs/SECURITY.md) for the threat model and the deployment
hardening checklist.
