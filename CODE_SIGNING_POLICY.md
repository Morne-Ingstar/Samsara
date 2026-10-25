# Code signing policy

Free code signing is provided by [SignPath.io](https://signpath.io/), and the
code signing certificate is provided by [SignPath Foundation](https://signpath.org/).
This policy describes the intended release process; Foundation approval and
account configuration are pending. Existing unsigned downloads remain unsigned.

## Team and review

| Role | Member | Permissions |
| --- | --- | --- |
| Author | [Morne](https://github.com/Morne-Ingstar) | [Repository access](https://github.com/Morne-Ingstar/Samsara/settings/access) |
| Reviewer | [Morne](https://github.com/Morne-Ingstar) | [Repository access](https://github.com/Morne-Ingstar/Samsara/settings/access) |
| Approver | [Morne](https://github.com/Morne-Ingstar) | [Repository access](https://github.com/Morne-Ingstar/Samsara/settings/access) |

The repository access page is visible to administrators. Morne currently holds
all three roles. Contributions from non-committers require review before release.
All team members must use MFA on GitHub and SignPath. Every signing request
requires manual approval under the `release-signing` policy, including both
requests in a release build.

## Release integrity

Once signing is enabled, every release is signed from a GitHub Actions build of
the tagged commit on GitHub-hosted runners. Only our own binaries built from
this repository's source are submitted for signing: `Samsara.exe`,
`samsara-mcp.exe`, and the resulting `SamsaraSetup-<version>.exe` installer.
Third-party DLLs, Python extension modules, and optional component archives are
not submitted for signing under our project identity.

The two application executables are signed before the portable ZIP and installer
are built. The installer is then signed separately. SHA-256 sidecars describe
the final downloadable files. All own PE files use ProductName `Samsara` and the
release's ProductVersion, derived from `samsara/__init__.py`.

## Privacy

See the [privacy policy](PRIVACY.md) for the application's data handling and
optional network services. Release signing submits build artifacts and build
origin information to SignPath; it does not submit users' profiles or recordings.

README / website section:
## Code signing policy
Read our [Code signing policy](CODE_SIGNING_POLICY.md) for signing status,
SignPath.io and SignPath Foundation attribution, team roles, and privacy policy.