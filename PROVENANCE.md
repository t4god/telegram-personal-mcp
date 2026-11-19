# Provenance and license review

The project owner authorized preparation and public release of their Telegram MCP. Ten Python modules were exported from their deployed connector source tree, without deployment secrets, personal data, backups or git history. Public-release scaffolding, setup, tests and callback configuration were added during preparation.

A review of those source files found no third-party copyright/license notices or stated copied/derived source blocks. This is a limited source-content inspection, not independent proof of authorship or an audit of historical provenance. No original repository history was imported or inspected. MIT was selected for this project under the owner's publication instruction; any contrary ownership information must be resolved before release.

Direct dependencies are installed separately, not vendored. Installed package metadata reviewed during preparation:

| Package | Version reviewed | License |
|---|---|---|
| mcp | 2.3.0 | MIT |
| Telethon | 1.46.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| python-multipart | 0.0.34 | Apache-2.0 |

No incompatible license was identified in this limited review. These dependencies retain their own licenses. MIT for this project does not relicense dependencies. Container distribution includes dependencies: retain installed package license notices and review the complete dependency inventory for release requirements.
