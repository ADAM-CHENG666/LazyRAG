//go:build windows

package codexhost

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
)

// Include hidden/tray main processes when deciding whether startup is safe.
// Never silently send new startup arguments to an existing non-CDP instance.
const windowsDesktopFunctions = `
function Start-CodexDesktop {
    $exe = $env:LAZYMIND_CODEX_DESKTOP
    $running = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $exe })
    foreach ($instance in $running) {
        if (-not $instance.CommandLine) { throw 'Cannot inspect Codex; quit it from its tray menu and retry' }
        if ($instance.CommandLine -match '(?:^|\s)--type(?:=|\s)') { continue }
        $process = Get-Process -Id $instance.ProcessId -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        if ($process.MainWindowHandle -eq 0) { throw 'Codex is still running in the tray; quit Codex from its tray menu and retry' }
        if (-not $process.CloseMainWindow()) { throw 'Close Codex and retry' }
        if (-not $process.WaitForExit(10000)) { throw 'Codex did not exit; quit it from its tray menu or finish its pending dialog and retry' }
    }
    # This explicit installation restart uses the same flags as the persistent shortcuts.
    Start-Process -FilePath $exe -ArgumentList @('--remote-debugging-address=127.0.0.1', ('--remote-debugging-port='+$env:LAZYMIND_CODEX_CDP_PORT))
}
`

func launchDesktop(ctx context.Context, application string, port int) error {
	// Pass the installed executable via environment, never interpolate a path
	// into PowerShell source. CloseMainWindow allows ordinary application shutdown.
	script := "$ErrorActionPreference='Stop';" + windowsDesktopFunctions + "\nStart-CodexDesktop"
	command := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script)
	command.Env = append(os.Environ(), "LAZYMIND_CODEX_DESKTOP="+application, "LAZYMIND_CODEX_CDP_PORT="+strconv.Itoa(port))
	if output, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("start Codex: %s: %w", output, err)
	}
	return nil
}
