# Dependency and TLS trust policy

Python dependencies are declared in `pyproject.toml`, locked in `uv.lock`, and installed with `uv sync --locked`. Both files are required for installation, updates, health checks and controller archives. Each Rust component uses its own committed `Cargo.lock`; builds and tests use `--locked`.

The controller installer, Python development group and CI use uv 0.13.6. Changing that pin requires updating all consumers and `scripts/check_tls_dependencies.py`. An explicit `NODE_PLANE_UV_BIN` is an administrator-supplied tool override; its owner must ensure it meets the security floor.

The reviewed minimums for the October 2026 emSign incident are certifi 2026.10.19, uv 0.13.4, webpki root bundles 1.0.10 and rustls-webpki 0.103.16. The lockfile checker rejects older versions and requires review for a new rustls-webpki minor series. The webpki-roots 0.26 compatibility wrapper is acceptable only when it delegates to a reviewed 1.x bundle. These checks address the known advisories; they are not a general vulnerability scanner.

Sources: [certifi advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2), [uv advisory](https://github.com/astral-sh/uv/security/advisories/GHSA-hq85-5cpj-mr62), [rustls-webpki advisory](https://rustsec.org/advisories/RUSTSEC-2026-0382.html).

Telegram's default Aiogram session loads certifi explicitly. Workstation HTTPS discovery/downloads and self-updates use Rust TLS clients; review both vendored-root and platform-verifier dependency paths. Driver/Agent mTLS uses the installation CA and remains a separate trust boundary. Host curl, Python's standard HTTPS client, platform verifiers and explicit custom CA settings depend on administrator-maintained host trust stores; updating certifi alone does not update those stores.

For a security update, resolve targeted packages from the authenticated registry, regenerate lockfiles and third-party license notices, build the affected consumers and test their actual TLS configuration. Rebuild/redeploy binaries and recreate or sync Python release environments before claiming a deployed installation is fixed. Never disable TLS verification to fetch an update.

Driver/Agent release downloads require curl, with HTTPS-only protocol and redirect restrictions, size limits and timeouts. Before extraction, both server archives must match the selected release's `SHA256SUMS.txt` entries and the accepted archive layout. Custom HTTPS archive URLs require explicit `NODE_PLANE_DRIVER_SHA256` / `NODE_PLANE_AGENT_SHA256` digests. Raw binaries are not accepted by this installer. Release checksums and HTTPS rely on the release publisher's integrity; they are not an independent publisher signature.
