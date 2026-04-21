# Documentation Publishing

Maxwell-Daemon publishes its MkDocs site through GitHub Pages. Pull requests
that touch documentation inputs build the site in strict mode so broken links,
missing nav entries, and invalid MkDocs configuration fail before merge.

## Pull Request Validation

The docs workflow runs for changes under `docs/`, `mkdocs.yml`,
`pyproject.toml`, or the docs workflow itself.

```bash
python -m pip install -e ".[docs]"
python -m mkdocs build --strict --site-dir site
```

Run the same commands locally when editing documentation pages, navigation, or
MkDocs extensions.

## Release Publishing

When a GitHub release is published, the workflow builds the site, uploads the
generated `site/` directory as a Pages artifact, and deploys it to the
repository's GitHub Pages environment.

Before publishing a release:

- Confirm the release branch has a passing docs build.
- Confirm `site_url` in `mkdocs.yml` matches the repository Pages URL.
- Review the quick-start, deployment, API, and configuration pages for version
  drift against the release notes.
- Keep videos and other externally hosted tutorials linked from docs pages
  rather than checking large media files into the repository.

## Manual Republish

Maintainers can run the docs workflow manually from GitHub Actions when Pages
needs to be republished without cutting a new release. Manual runs use the same
strict build and Pages deploy path as release publication.
