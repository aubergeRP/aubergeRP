# Security Policy

## Supported Versions

Only the latest release is actively maintained and receives security fixes.

| Version | Supported |
| :------ | :-------- |
| latest  | Yes       |
| older   | No        |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report security issues privately via [GitHub's private vulnerability reporting](https://github.com/aubergerp/aubergerp/security/advisories/new), or email **odoucet@oxeva.fr** with the subject line `[aubergeRP] Security`.

Include:
- A description of the vulnerability and its impact.
- Steps to reproduce (proof of concept if possible).
- Affected versions.

You can expect an acknowledgement within **72 hours** and a fix or mitigation plan within **14 days** for critical issues.

## Security Design Notes

- **Admin panel** is password-protected with bcrypt + JWT. The `AUBERGE_DISABLE_ADMIN_AUTH=1` env var bypasses all admin auth — never use it in production.
- **Chat UI** has no user authentication by default (see TODO for planned `app.auth_mode`). Do not expose AubergeRP directly to the internet without an auth proxy or VPN unless you accept that anyone can chat.
- **`custom_header_html` / `custom_footer_html`** are rendered as raw HTML (admin-only). Anyone with admin access can inject arbitrary JavaScript via these fields — treat admin credentials accordingly.
- **LLM responses** are sanitized with DOMPurify before being rendered as Markdown. If you use a self-hosted model, a deliberately crafted model output cannot inject scripts into the UI.
