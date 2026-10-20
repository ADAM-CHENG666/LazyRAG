package workbuddyhost

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
)

func launchDesktop(ctx context.Context, application string, port int) error {
	// Pass the installed executable via environment, never interpolate a path
	// into PowerShell source. CloseMainWindow allows ordinary application shutdown.
	script := `$ErrorActionPreference='Stop'; $exe=$env:LAZYMIND_WB_DESKTOP; Get-Process | Where-Object { $_.Path -eq $exe -and $_.MainWindowHandle -ne 0 } | ForEach-Object { if (-not $_.CloseMainWindow()) { throw 'Close WorkBuddy and retry' }; if (-not $_.WaitForExit(10000)) { throw 'WorkBuddy did not close; finish its pending dialog and retry' } }; Start-Process -FilePath $exe -ArgumentList @('--remote-debugging-address=127.0.0.1', ('--remote-debugging-port='+$env:LAZYMIND_WB_CDP_PORT))`
	command := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script)
	command.Env = append(os.Environ(), "LAZYMIND_WB_DESKTOP="+application, "LAZYMIND_WB_CDP_PORT="+strconv.Itoa(port))
	if output, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("start WorkBuddy: %s: %w", output, err)
	}
	return nil
}
