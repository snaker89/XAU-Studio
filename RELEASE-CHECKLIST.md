# Release checklist

The owner authorized a private, source-only repository without a title catalogue. Public visibility and binary release are not enabled by this checklist.

- [x] Prepare a separate source tree without private accounts, tokens, progress, or logs.
- [x] Add English documentation, GPLv3 license, upstream attribution, and third-party notices.
- [x] Publish without a title catalogue; document user-supplied CSV import.
- [x] Keep all private title catalogues and account fingerprints out of Git.
- [ ] Make portable packaging reproducible with explicit, pinned build dependencies and complete runtime notices.
- [ ] Build and test the exact release commit; test a clean Windows user profile.
- [ ] Scan repository, Git history, source archives, payload archives, and executable inputs for private data.
- [ ] Create and verify private `snaker89/XAU-Studio`. Public visibility requires a separate instruction.
- [ ] Tag `v1.0.0-beta.1`, attach the Windows x64 EXE, corresponding source archive, notices, and SHA256SUMS.txt to a prerelease.
- [ ] Verify uploaded assets and their checksums against the local files.

EXEs belong in GitHub Releases, not in Git history. Every binary must have corresponding source and build instructions. Do not claim code signing unless the binary is actually signed.
