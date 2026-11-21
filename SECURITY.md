# Security Policy

## Supported versions

Security fixes are applied to the latest release on the `main` branch.

## Reporting a vulnerability

Please do not report security vulnerabilities through public issues, discussions or pull requests.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Include:

- a description of the issue and its impact
- steps to reproduce, or a proof of concept
- the affected version or commit
- any suggested remediation

You can expect an acknowledgement within a few days. We will work with you to confirm the issue, prepare a fix, and agree a disclosure timeline.

## Scope and expectations

MediFlow AI ships with a synthetic demonstration dataset and is not certified for use with real patient data. Anyone deploying it with protected health information is responsible for completing the compliance work (for example HIPAA or India's DPDP Act) described in [docs/ROADMAP.md](docs/ROADMAP.md).

Deployment hardening guidance, including the production start-up checks and the post-deploy checklist, is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
