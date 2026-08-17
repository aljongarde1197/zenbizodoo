# Claude–Odoo 19 OAuth Read-Only MCP Connector

OAuth-enabled version of the read-only Odoo 19 MCP connector.

```text
Claude
  -> OAuth 2.1 access token
  -> Python MCP Resource Server
  -> Odoo JSON-2 API
```

## Important architecture

The Python service is an OAuth **Resource Server**. It validates bearer tokens
issued by an external OAuth/OIDC Authorization Server such as Microsoft Entra
ID, Auth0, Okta, or another compatible identity provider.

The Odoo API key stays only on the Python connector server.

## Odoo tools

- test_odoo_connection
- search_crm_opportunities
- get_crm_opportunity
- search_sales_orders
- get_sales_order
- search_products
- get_stock_by_location
- search_inventory_transfers

No Odoo create/update/delete/confirm/validate/cancel tools are included.

## Install on Windows

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## First local test

Keep:

```env
AUTH_ENABLED=false
TRANSPORT=streamable-http
HOST=0.0.0.0
PORT=8000
```

Run:

```powershell
python test_connection.py
python -m unittest discover -s tests
python -m app.server
```

Then test `http://127.0.0.1:8000/mcp` with MCP Inspector.

## OAuth configuration

Register an API/resource and OAuth client with the client's identity provider.
The provider must issue JWT access tokens containing:

- `iss` matching AUTH_ISSUER_URL
- `aud` matching AUTH_AUDIENCE
- `exp`
- required scope, default `odoo.read`

Then configure:

```env
AUTH_ENABLED=true
AUTH_ISSUER_URL=https://auth.example.com
AUTH_RESOURCE_SERVER_URL=https://YOUR_PUBLIC_HOST/mcp
AUTH_JWKS_URL=https://auth.example.com/.well-known/jwks.json
AUTH_AUDIENCE=https://YOUR_PUBLIC_HOST/mcp
AUTH_REQUIRED_SCOPES=odoo.read
AUTH_ALGORITHMS=RS256
```

Restart:

```powershell
python -m app.server
```

The MCP SDK will publish protected-resource metadata used by Claude for OAuth
discovery. The Authorization Server itself remains external.

## Claude

Add:

```text
https://YOUR_PUBLIC_HOST/mcp
```

If the identity provider does not support dynamic client registration, add
the OAuth Client ID and Client Secret in Claude connector Advanced settings.

Each authorized Claude user then clicks Connect and signs in through the
configured identity provider.

## Dev Tunnel

A public VS Code Dev Tunnel can be used temporarily for staging. If its URL
changes, update AUTH_RESOURCE_SERVER_URL and usually AUTH_AUDIENCE as well.

Do not leave AUTH_ENABLED=false on a publicly accessible persistent MCP
deployment.
# zenbizodoo
