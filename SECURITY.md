# Security

- OAuth mode validates JWT signature, issuer, audience, expiration and scopes.
- Use a trusted external OAuth/OIDC provider.
- Do not disable audience validation.
- Keep AUTH_ENABLED=true for persistent remote deployments.
- Keep the Odoo API key only in `.env` or a secret manager.
- Use a dedicated read-only Odoo integration account.
- Test with staging first.
- Review multi-company access and returned fields.
- A public VS Code Dev Tunnel is for temporary testing only.
