# Contributing

Bug reports and pull requests are welcome.

- **Bugs:** open an issue with your image tag, a redacted compose file, and logs with `LOG_LEVEL=DEBUG`.
- **Pull requests:** keep them focused, and add or update a test in `tests/` for behaviour changes. CI runs the tests and builds the image for amd64 and arm64.
- **Compatibility:** existing environment variables must keep working, since people switch over from the original tiredofit image without changing their config.

## Running locally

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests
docker build -t traefik-cloudflare-companion:dev .
```

## Releases

Pre-releases are tagged `X.Y.Z-BETA` and published as `:X.Y.Z-BETA` and `:beta`. Stable releases are tagged `X.Y.Z` and published as `:X.Y.Z`, `:X.Y`, `:X` and `:latest`. Every release has an entry in [CHANGELOG.md](CHANGELOG.md).
