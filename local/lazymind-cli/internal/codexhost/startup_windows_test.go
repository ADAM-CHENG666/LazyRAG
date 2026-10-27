package codexhost

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWindowsShortcutInstallationRestoresOriginalBytes(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "state")
	desktop := filepath.Join(root, "Desktop.lnk")
	pinned := filepath.Join(root, "Pinned.lnk")
	_ = os.WriteFile(pinned, []byte("original shortcut"), 0600)
	previous := startupRunWindows
	startupRunWindows = func(_ context.Context, script string, _ []string) ([]byte, error) {
		if script != windowsShortcutPlan {
			return []byte("ok"), nil
		}
		return json.Marshal([]map[string]any{{"path": desktop, "body": []byte("managed desktop")}, {"path": pinned, "body": []byte("managed pinned")}})
	}
	t.Cleanup(func() { startupRunWindows = previous })
	s := startupRecord{Application: filepath.Join(root, "ChatGPT.exe"), Port: 9334}
	for i := 0; i < 2; i++ {
		if err := configureStartup(context.Background(), home, &s, ""); err != nil {
			t.Fatal(err)
		}
	}
	if err := checkStartup(context.Background(), home, s); err != nil {
		t.Fatal(err)
	}
	if err := disableStartup(context.Background(), home, s); err != nil {
		t.Fatal(err)
	}
	body, _ := os.ReadFile(pinned)
	if string(body) != "original shortcut" {
		t.Fatal("original pin lost")
	}
	if _, err := os.Stat(desktop); !os.IsNotExist(err) {
		t.Fatal("created shortcut left behind")
	}
}

func TestWindowsShortcutScriptWithRealCOMInIsolatedFolders(t *testing.T) {
	root := t.TempDir()
	desktop := filepath.Join(root, "Desktop")
	programs := filepath.Join(root, "Programs")
	pinned := filepath.Join(root, "TaskBar")
	for _, dir := range []string{desktop, programs, pinned} {
		if err := os.MkdirAll(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}
	// Replace folder discovery only. All real Shell COM writes remain inside TempDir.
	script := strings.ReplaceAll(windowsShortcutPlan, "[Environment]::GetFolderPath('DesktopDirectory')", "$env:FIXTURE_DESKTOP")
	script = strings.ReplaceAll(script, "[Environment]::GetFolderPath('Programs')", "$env:FIXTURE_PROGRAMS")
	script = strings.ReplaceAll(script, "Join-Path $env:APPDATA 'Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar'", "$env:FIXTURE_PINNED")
	launcher := filepath.Join(root, "state with space", "launch.ps1")
	env := []string{"FIXTURE_DESKTOP=" + desktop, "FIXTURE_PROGRAMS=" + programs, "FIXTURE_PINNED=" + pinned, "LAZYMIND_CODEX_APP=" + filepath.Join(root, "ChatGPT.exe"), "LAZYMIND_CODEX_LAUNCHER=" + launcher}
	body, err := startupRunWindows(context.Background(), script, env)
	if err != nil {
		t.Fatal(err)
	}
	var entries []struct {
		Path string `json:"path"`
		Body []byte `json:"body"`
	}
	if json.Unmarshal(body, &entries) != nil || len(entries) != 2 {
		t.Fatalf("invalid plan: %s", body)
	}
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Path, root+string(os.PathSeparator)) {
			t.Fatal("escaped fixture")
		}
		if err := os.WriteFile(entry.Path, entry.Body, 0600); err != nil {
			t.Fatal(err)
		}
	}
	// Parsing the generated launch script catches PowerShell errors without launching Codex.
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", `$errors=$null; $tokens=$null; [System.Management.Automation.Language.Parser]::ParseInput($env:FIXTURE_SCRIPT,[ref]$tokens,[ref]$errors) | Out-Null; if ($errors.Count) { throw ($errors | Out-String) }`)
	cmd.Env = append(os.Environ(), "FIXTURE_SCRIPT="+windowsPersistentLauncher)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("invalid launcher %s %v", out, err)
	}
}

func TestWindowsPersistentLauncherHonorsPreferenceAndUnicodePaths(t *testing.T) {
	root := filepath.Join(t.TempDir(), "中文目录 with space")
	if err := os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	exe := filepath.Join(root, "FakeCodex.cmd")
	if err := os.WriteFile(exe, []byte("@echo off\r\n> \"%~dp0launched.txt\" echo [%*]\r\n"), 0600); err != nil {
		t.Fatal(err)
	}
	launcher := filepath.Join(root, "launch.ps1")
	if err := os.WriteFile(launcher, []byte(windowsPersistentLauncher), 0600); err != nil {
		t.Fatal(err)
	}
	config, _ := json.Marshal(startupRecord{Application: exe, Port: 9334})
	if err := os.WriteFile(filepath.Join(root, "startup.json"), config, 0600); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(root, "launched.txt")
	for _, enabled := range []bool{true, false} {
		body, _ := json.Marshal(enabled)
		if err := os.WriteFile(filepath.Join(root, "enabled.json"), body, 0600); err != nil {
			t.Fatal(err)
		}
		_ = os.Remove(marker)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		out, err := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", launcher).CombinedOutput()
		cancel()
		if err != nil {
			t.Fatalf("launcher failed: %s %v", out, err)
		}
		deadline := time.Now().Add(5 * time.Second)
		for {
			body, err = os.ReadFile(marker)
			if err == nil {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("launcher never started fixture")
			}
			time.Sleep(20 * time.Millisecond)
		}
		got := strings.TrimSpace(string(body))
		if enabled {
			if !strings.Contains(got, "--remote-debugging-port=9334") || !strings.Contains(got, "--remote-debugging-address=127.0.0.1") {
				t.Fatalf("missing startup flags: %s", got)
			}
		} else if got != "[]" {
			t.Fatalf("disabled launcher still supplied flags: %s", got)
		}
	}
}
