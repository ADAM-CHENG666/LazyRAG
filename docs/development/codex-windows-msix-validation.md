# Windows Codex MSIX discovery validation

## Change

Windows desktop discovery keeps the existing order: explicit desktop binding,
App Paths / protocol / Uninstall registration, then MSIX. For MSIX, it queries
the current user's `OpenAI.Codex` package with Windows PowerShell's Appx module
and reads application executables from its manifest. It resolves the executable
against the returned `InstallLocation`, including `app\ChatGPT.exe`, without
embedding a package version, install drive, or AppData location.

Only an existing desktop executable from the matching package counts. A CLI on
PATH, a stale package, an unreadable manifest, or a failed/timed-out query does
not count as an installed desktop. No package is installed, launched or changed
by this discovery query. Codex configuration and integration enablement are
unchanged.

## Build and verification boundary

The supplied Windows x64 ZIP is a connector hotfix, not a full desktop installer.
It contains `lazymind.exe`, `agentexec.test.exe`, this guide, and build/hash
metadata. The Electron UI uses this connector for Codex detection, so it can be
tested with the existing PR #835 Windows desktop installation.

macOS can run the portable discovery/adapter tests and cross-compile Windows
binaries. Windows PowerShell, Appx enumeration, registry discovery, WindowsApps
file access and the settings UI still require Windows execution. Cross-compilation
is not evidence of a successful Windows test run.

## Windows reproduction and acceptance

Use the same user and the same launch environment as the affected desktop test,
including its existing `LOCALAPPDATA` / `APPDATA` overrides. Do not add a desktop
binding or enable Codex integration to work around detection.

1. Extract the hotfix ZIP to a separate directory. Verify its hashes against
   `SHA256SUMS.txt` with `Get-FileHash -Algorithm SHA256`.
2. In **Windows PowerShell**, record the real package and manifest:

   ```powershell
   $packages = @(Get-AppxPackage -Name OpenAI.Codex)
   $packages | Select-Object Name, Version, PackageFullName, InstallLocation
   foreach ($package in $packages) {
       $manifest = Get-AppxPackageManifest -Package $package.PackageFullName
       foreach ($application in $manifest.Package.Applications.Application) {
           $exe = Join-Path $package.InstallLocation $application.Executable
           [PSCustomObject]@{ Executable = $exe; Exists = Test-Path -LiteralPath $exe -PathType Leaf }
       }
   }
   ```

3. Run the hotfix connector's status query from the extracted directory:

   ```powershell
   .\lazymind.exe internal agent codex status
   ```

   Expect `codex_desktop.satisfied = true`. With the existing initialized
   `.codex` profile (or existing `CODEX_HOME` override), expect
   `codex_desktop_initialized.satisfied = true`. This command does not connect
   Codex or rewrite its configuration. LazyMind may create its own local host
   identity if the selected LazyMind data directory has never been initialized.

4. For UI testing, fully exit **LazyMind** and retain the existing Windows test
   launcher's data-isolation settings. In that launcher's environment, set only:

   ```powershell
   $env:LAZYMIND_DESKTOP_AGENT_CONNECTOR = (Resolve-Path .\lazymind.exe).Path
   # Launch the existing LazyMind test desktop with its usual executable/launcher.
   ```

   This process-scoped override selects the hotfix without replacing the installed
   binary. Open Codex settings and refresh status. The two incorrect desktop
   warnings should disappear. Do not click Connect/Enable for this acceptance test.
   Remove the override (or close this PowerShell session) and restart LazyMind to
   return to the installed connector.

5. Run the packaged discovery tests:

   ```powershell
   .\agentexec.test.exe -test.v -test.run 'TestWindowsDesktopPackage|TestWindowsAppPackage|TestDesktopManualBinding|TestAppPackageQuery'
   ```

   These cover MSIX manifest resolution, package upgrades, traditional-install
   priority, manual-binding priority, CLI-only/no-install cases, query failures,
   malformed results, and invalid executable paths. Windows-only tests exercise
   the PowerShell JSON/XML protocol and a real Appx query for an absent identity.
   Fixtures use temporary data; no Codex settings are changed.

6. On a machine/profile with traditional installation, verify that its prior
   desktop path still wins. On a CLI-only machine/profile, desktop requirements
   must remain unsatisfied. These cases can also be checked through the fixtures
   without uninstalling applications from the original user's machine.

Record the ZIP build commit, Windows version, PowerShell version, command output,
and settings-page result. A full Windows installer can subsequently be built
from the same commit using `.github/workflows/windows-installer.yml`.

## References

- [Get-AppxPackage: packages installed in a user profile](https://learn.microsoft.com/en-us/powershell/module/appx/get-appxpackage)
- [Application manifest executable](https://learn.microsoft.com/en-us/uwp/schemas/appxpackage/uapmanifestschema/element-application)
