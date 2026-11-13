# Release procedure

Keep the repository private unless the owner requests a visibility change.

1. Update the version and changelog; run the build, Python tests and application self-tests.
2. Review tracked files for private data. Exclude accounts, sessions, progress, logs and title catalogues.
3. Commit and build with `studio/Build-Portable.ps1` from that exact commit.
4. Inspect the payload and run `--portable-check`; verify an empty bundled catalogue and a working Python worker.
5. Archive corresponding source and licenses, including the Python runtime license; generate SHA256SUMS.txt.
6. Tag the commit, publish the release, download its assets and verify all checksums.

EXEs belong in Releases, not Git history. The executable is unsigned. Mock tests do not verify live Microsoft sign-in or Xbox service availability.
