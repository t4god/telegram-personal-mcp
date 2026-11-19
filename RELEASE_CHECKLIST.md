# Release checklist

- [x] Select MIT for the exported project; record provenance and its limits in PROVENANCE.md.
- [x] Complete independent offline review (15 tests); real-account interoperability remains unverified.
- [ ] Review exported source and dependency licenses.
- [x] Run tests (15 passed) and secret scans (including detect-secrets, zero findings).
- [ ] Publish only this clean tree, never the production directory or its history.
- [ ] Validate setup on a disposable account and clean host with HTTPS.
- [ ] Validate the intended MCP client's OAuth and send confirmation experience.
- [ ] Verify webhooks with the chosen client; do not imply universal support.
- [ ] Set repository description, contact, version tag and changelog.

This preparation was copied from source modules only. No production configuration, sessions, databases, logs, backups or original git history were exported. Live account operations were not exercised during offline tests.
