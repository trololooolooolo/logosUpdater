# Contributing

Thanks for helping! A few rules keep this project safe to host publicly.

## Never attach car or logo files

Do **not** attach to issues or pull requests:

- `station_logos.img` or any other image from a head unit,
- `fm_station_logo.db` / `dab_station_logo.db`,
- station logo files (PNG) or screenshots where real station logos are readable,
- firmware files, `Version.inf` dumps, VINs, or anything that identifies your car.

These belong to Ford, to broadcasters or to you. The `.gitignore` already blocks the
usual file names — please don't force-add them. If a bug needs a reproduction, describe
the steps and paste the log output (`sync_log.txt`) instead; anonymise station IDs if
you prefer.

## Reporting a bug

Include:

- your operating system and Python version (or the release package you used),
- what you did, what you expected, what happened,
- the relevant part of `sync_log.txt` (run with `--debug` for more detail).

## Pull requests

- Keep changes focused; one topic per PR.
- New UI text goes into **both** `lang/en.yaml` and `lang/pl.yaml` (see `docs/I18N.md`).
- Data-source plugins: see `docs/SOURCES.md`. A plugin must not ship scraped data or logos.
- Device-side code (`qnx6fs.py`, `ford_db.py`, `usb_package.py`) affects what gets written
  to a car — describe how you tested it (round-trip build, kernel mount, a real car).
- For larger changes, open an issue first and describe the plan (what changes, why, how you will test it).

By contributing you agree that your contribution is licensed under the MIT license of this project.
