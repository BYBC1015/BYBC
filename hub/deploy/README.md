# Deployment examples

These are portable configuration examples. Set your own host, credentials and data directory.
The example endpoint is reserved for documentation and does not connect to the original deployment.

1. Install this source with Python 3.10+ and its declared dependencies.
2. Set CODEX_HUB_DATA_DIR, CODEX_HUB_SECRET_KEY and CODEX_HUB_OWNER_PASSWORD in a private environment file.
3. Configure the systemd service and Nginx example for your own account and host.
4. Back up application code, the SQLite database, uploads and private configuration before upgrading.
5. Start the service; verify login, workspace, attachments, task execution and feedback.

See [weekly maintenance](MAINTENANCE.md) and [release notes](RELEASE-2.9.3.md).
The optional portable-backup script creates sensitive local archives; never commit its output.
Real environment files, authentication state, backups and user uploads are excluded from this source snapshot.
