# Releasing a new version (for the maintainer)

1. Set `APP_VERSION` at the top of `SWhereUsed.py` to the new number, for example `1.34.0`, and add a section to
   `CHANGELOG.md`.
2. Run the tests: `python SWhereUsed.py --selftest`.
3. Make the zip: the folder `SWhereUsed` with everything in it (not `__pycache__`), named `SWhereUsed-1.34.0.zip`.
   Install update looks for `SWhereUsed.py` and `SWhereUsed.html` in it.
4. On GitHub: **Releases, Draft a new release**. Tag `v1.34.0`, title `SWhereUsed 1.34.0`, paste the changelog
   section as the description (users see it under "What is new"), and attach the zip. Publish.

`UPDATE_REPO` at the top of `SWhereUsed.py` must name the repository (for example `stefansterk/SWhereUsed`).
Within twelve hours every SWhereUsed that checks for updates sees the new version; Check for updates sees it at once.
