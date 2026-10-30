# ZIP deployment snapshot

This folder keeps the source code from the locally deployed ZIP app for reference. The repository root contains the current project and its Docker setup; this older app is not part of that Docker image.

The archive's local `.env`, SQLite databases, uploaded review photos, backups, and Python cache files are intentionally absent. Admin and demo credentials, the Telegram bot token, and the manager's Telegram ID must be provided through environment variables. Authentication stays disabled until a non-empty password is configured.

To run this snapshot locally, install `requirements.txt`, set configuration in a private `.env` file in this directory, then run `uvicorn app:app --host 127.0.0.1 --port 8001` from this directory. Keep `.env`, `database.db`, `database_old.db`, `backups/`, and `static/uploads/` private.
