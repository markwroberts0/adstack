# Security policy

Adstack holds credentials for advertising accounts and can change what those accounts spend, so we take security reports seriously.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. Don't open a public issue. Include the Adstack version, the steps to reproduce and the impact you expect. We'll acknowledge the report, keep you updated, and credit you in the release notes if you'd like.

## Supported versions

Adstack is in alpha. Security fixes go into the latest release only.

## Scope

In scope, for example:

- ways to make Adstack change an account without an approved, current plan or without the permission flags the change requires;
- ways for an MCP client to read files outside the server's `--root`, apply changes when `apply` isn't enabled, or apply a plan the server didn't create;
- credentials leaking into plan files, receipts, logs or error messages.

Out of scope: vulnerabilities in the ad platforms themselves, and the consequences of configurations you approve.

## Handling credentials safely

- Keep OAuth credentials in your environment or a secret manager. Never commit them; `.gitignore` already excludes `.env` files and common credential file names.
- Plan files and receipts contain account IDs and campaign details. Treat them as private operational data and keep them out of version control (`.adstack/` and `*.plan.json` are ignored by default).
- In CI, don't expose ad platform credentials to workflows that run untrusted pull request code.
- Prefer `adstack mcp` without `--allow-apply` unless you want an assistant to apply plans, and configure your MCP client to ask before every `apply`.
