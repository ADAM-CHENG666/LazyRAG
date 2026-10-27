package codexhost

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"unsafe"

	"golang.org/x/sys/windows"
	"lazymind/agentconnector/internal/desktopsetup"
)

// Resolve Store updates at launch time; the versioned installation directory
// must not be permanently baked into shortcuts. Never stop an existing app.
const windowsPersistentLauncher = `param()
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'startup.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$exe = $config.application
if ($exe -match '\\WindowsApps\\OpenAI\.Codex_') {
 $package = Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1
 if ($package) {
  foreach ($name in @('ChatGPT.exe','Codex.exe')) {
   $candidate = Join-Path $package.InstallLocation ('app\'+$name)
   if (Test-Path -LiteralPath $candidate) { $exe = $candidate; break }
  }
 }
}
if (-not (Test-Path -LiteralPath $exe)) { throw 'Codex installation is missing. Turn its LazyMind switch off and on again.' }
$choice = Join-Path $PSScriptRoot 'enabled.json'
if ((Test-Path -LiteralPath $choice) -and (Get-Content -LiteralPath $choice -Raw).Trim() -ne 'true') { Start-Process -FilePath $exe; exit }
Start-Process -FilePath $exe -ArgumentList @('--remote-debugging-address=127.0.0.1', ('--remote-debugging-port='+$config.port))
`

// Produce shortcut bytes in a temporary directory. Go journals originals before
// applying the bytes. WScript.Shell is never used to save an unjournaled live link.
const windowsShortcutPlan = `
$shell = New-Object -ComObject WScript.Shell
$shellApp = New-Object -ComObject Shell.Application
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$programs = [Environment]::GetFolderPath('Programs')
$pinned = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
$roots = @($desktop,$programs,$pinned)
$paths = @{}
foreach ($root in $roots) {
 if (-not (Test-Path -LiteralPath $root)) { continue }
 foreach ($file in Get-ChildItem -LiteralPath $root -Filter '*.lnk' -File -Recurse) {
  $link = $shell.CreateShortcut($file.FullName)
  $target = $link.TargetPath
  $item = $shellApp.NameSpace($file.DirectoryName).ParseName($file.Name)
  $appId = $item.ExtendedProperty('System.AppUserModel.ID')
  $packaged = ($target -match 'OpenAI\.Codex_' -or $link.Arguments -match 'shell:AppsFolder\\OpenAI\.Codex_' -or $appId -match '^OpenAI\.Codex_')
  if ($target -eq $env:LAZYMIND_CODEX_APP -or ($env:LAZYMIND_CODEX_PREVIOUS -and $target -eq $env:LAZYMIND_CODEX_PREVIOUS) -or $packaged -or $link.Arguments.Contains($env:LAZYMIND_CODEX_LAUNCHER)) {
   # Custom workspace links are not ordinary launch entries; preserve them.
   if (-not $packaged -and $link.Arguments -and -not $link.Arguments.Contains($env:LAZYMIND_CODEX_LAUNCHER) -and $link.Arguments -notmatch '^--remote-debugging-') { continue }
   $paths[$file.FullName] = $true
  }
 }
}
foreach ($root in @($desktop,$programs)) { $paths[(Join-Path $root 'Codex (LazyMind).lnk')] = $true }
$result = @()
$temp = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
[IO.Directory]::CreateDirectory($temp) | Out-Null
try {
 foreach ($path in $paths.Keys) {
  $copy = Join-Path $temp ([Guid]::NewGuid().ToString()+'.lnk')
  # Build a fresh link: packaged-app activation metadata on the old link can
  # bypass TargetPath. Its complete original bytes are journaled by the caller.
  $link = $shell.CreateShortcut($copy)
  $link.TargetPath = Join-Path $PSHOME 'powershell.exe'
  $link.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+$env:LAZYMIND_CODEX_LAUNCHER+'"'
  $link.WorkingDirectory = Split-Path -Parent $env:LAZYMIND_CODEX_LAUNCHER
  $link.IconLocation = $env:LAZYMIND_CODEX_APP+',0'
  $link.Description = 'Codex with LazyMind Workflow'
  $link.Save()
  $result += @{ path=$path; body=[Convert]::ToBase64String([IO.File]::ReadAllBytes($copy)) }
 }
 ConvertTo-Json -InputObject @($result) -Compress
} finally { Remove-Item -LiteralPath $temp -Recurse -Force }
`

var startupRunWindows = func(ctx context.Context, script string, env []string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"+script)
	cmd.Env = append(os.Environ(), env...)
	body, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("configure Codex shortcuts: %s: %w", body, err)
	}
	return body, nil
}

func configureStartup(ctx context.Context, home string, s *startupRecord, previous string) error {
	s.Launcher = filepath.Join(home, "codex-workflow", "launch.ps1")
	files, err := desktopsetup.Load(filepath.Join(home, "codex-workflow", "startup-files.json"))
	if err != nil {
		return err
	}
	if err = saveStartup(home, *s); err != nil {
		return err
	}
	if err = files.Put(s.Launcher, []byte(windowsPersistentLauncher), 0600); err != nil {
		return err
	}
	body, err := startupRunWindows(ctx, windowsShortcutPlan, []string{"LAZYMIND_CODEX_APP=" + s.Application, "LAZYMIND_CODEX_PREVIOUS=" + previous, "LAZYMIND_CODEX_LAUNCHER=" + s.Launcher})
	if err != nil {
		return err
	}
	var entries []struct {
		Path string `json:"path"`
		Body []byte `json:"body"`
	}
	if json.Unmarshal(body, &entries) != nil || len(entries) < 2 {
		return fmt.Errorf("could not prepare Codex shortcuts")
	}
	for _, entry := range entries {
		if !filepath.IsAbs(entry.Path) || filepath.Ext(entry.Path) != ".lnk" {
			return fmt.Errorf("invalid shortcut path")
		}
		if err = files.Put(entry.Path, entry.Body, 0600); err != nil {
			return err
		}
		notifyShortcut(entry.Path)
	}
	return nil
}
func disableStartup(ctx context.Context, home string, s startupRecord) error {
	files, err := desktopsetup.Load(filepath.Join(home, "codex-workflow", "startup-files.json"))
	if err != nil {
		return err
	}
	paths := make([]string, 0, len(files.Entries))
	for path := range files.Entries {
		if filepath.Ext(path) == ".lnk" {
			paths = append(paths, path)
		}
	}
	if err = files.Restore(); err != nil {
		return err
	}
	for _, path := range paths {
		notifyShortcut(path)
	}
	return nil
}

func notifyShortcut(path string) {
	value, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return
	}
	// SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSH: Explorer reloads desktop
	// and taskbar links without restarting Explorer or the external application.
	windows.NewLazySystemDLL("shell32.dll").NewProc("SHChangeNotify").Call(0x2000, 0x1005, uintptr(unsafe.Pointer(value)), 0)
	runtime.KeepAlive(value)
}
func checkStartup(ctx context.Context, home string, s startupRecord) error {
	files, err := desktopsetup.Load(filepath.Join(home, "codex-workflow", "startup-files.json"))
	if err != nil {
		return err
	}
	if err = files.Check(); err != nil {
		return err
	}
	// Also catch shortcuts recreated by an app update or a new taskbar pin.
	body, err := startupRunWindows(ctx, `
$shell = New-Object -ComObject WScript.Shell
$roots = @([Environment]::GetFolderPath('DesktopDirectory'),[Environment]::GetFolderPath('Programs'),(Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'))
foreach ($root in $roots) {
 if (-not (Test-Path -LiteralPath $root)) { continue }
 foreach ($file in Get-ChildItem -LiteralPath $root -Filter '*.lnk' -File -Recurse) {
  $link = $shell.CreateShortcut($file.FullName)
  if (($link.TargetPath -eq $env:LAZYMIND_CODEX_APP -and -not $link.Arguments) -or $link.TargetPath -match 'OpenAI\.Codex_' -or $link.Arguments -match 'shell:AppsFolder\\OpenAI\.Codex_') { throw 'Codex startup entry no longer includes LazyMind configuration' }
 }
}
'ok'`, []string{"LAZYMIND_CODEX_APP=" + s.Application, "LAZYMIND_CODEX_PORT=" + strconv.Itoa(s.Port)})
	_ = body
	return err
}
