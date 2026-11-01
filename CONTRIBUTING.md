# Contributing

Open an issue before a larger change, so the approach can be agreed first.

1. Branch from `main` and keep one logical change per commit, using Conventional
   Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).
2. Add or adjust a test with every behavior change.
3. Run `make test` with Python 3.14 and uv. See [docs/development.md](docs/development.md).
4. Open a pull request that says what changed, why, and how you verified it.

Never use real credentials, listening history, room names, addresses or Last.fm
responses in tests, fixtures, logs or issues. Tests use synthetic data and local servers.

Features stay within scope: this integration reads Sonos and never controls it, and
sending to Last.fm stays off until the user turns it on.

Security problems go through [SECURITY.md](SECURITY.md), not a public issue.
