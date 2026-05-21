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

### Authentication

- **Admin panel** is password-protected with bcrypt password verification and short-lived signed JWTs.
- **Chat UI** has no user authentication by default (a full `app.auth_mode` is a planned feature). Do not expose AubergeRP directly to the internet without a reverse-proxy auth layer or VPN unless you accept that anyone who can reach the port can chat.

### `AUBERGE_DISABLE_ADMIN_AUTH`

> **Warning: never set this in production.**

Setting `AUBERGE_DISABLE_ADMIN_AUTH=1` completely bypasses admin authentication. Every `/api/admin/*` endpoint becomes publicly accessible with no credentials required — anyone who can reach the server can read and modify connectors, characters, configuration, and prompts.

This variable exists solely for local development and automated testing. It must not appear in any production deployment, Docker Compose file committed to a public repository, or cloud environment.

### Content Security Policy

AubergeRP sets the following CSP on every response:

```
script-src 'self'
style-src  'self' 'unsafe-inline'
```

`script-src` allows only same-origin scripts — inline `<script>` blocks and external CDN scripts are both blocked.

`style-src` requires `'unsafe-inline'` for one reason: the Admin → Customization panel lets administrators inject arbitrary CSS via a dynamically created `<style>` element. Eliminating this directive would require server-side nonce injection into the HTML, which is outside the current architecture. The trade-off is accepted because CSS injection cannot exfiltrate data or execute code on its own, and the feature is admin-only.

### Custom HTML fields

`custom_header_html` and `custom_footer_html` are injected as raw HTML into every page. Anyone with admin access can use these fields to execute arbitrary JavaScript in every visitor's browser. Treat admin credentials with the same care as server access credentials.

### LLM output

LLM responses are passed through [DOMPurify](https://github.com/cure53/DOMPurify) before being rendered as Markdown via `innerHTML`. This prevents a malicious or jailbroken model from injecting executable HTML into the chat UI.
