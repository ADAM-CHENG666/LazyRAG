package codexhost

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"lazymind/agentconnector/internal/agentexec"
	"lazymind/agentconnector/internal/desktopsetup"
	"lazymind/agentconnector/internal/localfile"
)

type startupRecord struct {
	Application string       `json:"application"`
	Port        int          `json:"port"`
	Launcher    string       `json:"launcher,omitempty"`
	Dock        []dockChange `json:"dock,omitempty"`
}
type dockChange struct {
	ID     int64           `json:"id"`
	Before json.RawMessage `json:"before,omitempty"`
}

func desktopApplication() (string, error) {
	return agentexec.FindDesktopApplication(agentexec.DesktopApplication{BindingTarget: agentexec.CodexDesktop, DisplayNames: []string{"ChatGPT", "Codex"}, ExecutableNames: []string{"ChatGPT.exe", "Codex.exe"}, Protocols: []string{"codex"}})
}

var startupApplication = desktopApplication
var startupRunning = desktopsetup.Running
var startupListenerMatches = desktopsetup.ListenerMatches
var startupDesktopReady = DesktopReady

func startupPath(home string) string { return filepath.Join(home, "codex-workflow", "startup.json") }
func readStartup(home string) (startupRecord, error) {
	var s startupRecord
	body, err := os.ReadFile(startupPath(home))
	if err != nil {
		return s, err
	}
	if json.Unmarshal(body, &s) != nil || !filepath.IsAbs(s.Application) || !filepath.IsAbs(s.Launcher) || s.Port < 1 || s.Port > 65535 {
		return s, fmt.Errorf("invalid Codex startup configuration")
	}
	return s, nil
}
func saveStartup(home string, s startupRecord) error {
	body, _ := json.Marshal(s)
	return localfile.Write(startupPath(home), body, 0600)
}

func ConfigureStartup(ctx context.Context, home string) error {
	unlock, err := desktopsetup.Lock(home, "codex")
	if err != nil {
		return err
	}
	defer unlock()
	c, err := readConfig(home)
	if err != nil {
		return err
	}
	application, err := desktopApplication()
	if err != nil {
		return err
	}
	s, err := readStartup(home)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	// Keep the recorded app while restoring original entries after an update.
	previous := s.Application
	s.Application, s.Port = application, c.Port
	return configureStartup(ctx, home, &s, previous)
}
func DisableStartup(ctx context.Context, home string) error {
	unlock, err := desktopsetup.Lock(home, "codex")
	if err != nil {
		return err
	}
	defer unlock()
	s, err := readStartup(home)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if err = disableStartup(ctx, home, s); err != nil {
		return err
	}
	return os.Remove(startupPath(home))
}

// Health checks are read-only. They never launch, repair or restart the desktop.
// Waiting is distinct from a running app with a broken startup/control channel.
func StartupHealth(ctx context.Context, home string) string {
	bounded, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	s, err := readStartup(home)
	if err != nil {
		return "startup_unavailable"
	}
	current, err := startupApplication()
	if err != nil || !sameStartupApplication(s.Application, current) {
		return "startup_unavailable"
	}
	// Store upgrades move the executable; the persistent launcher resolves the
	// same package dynamically, so inspect its current path, not the stale one.
	s.Application = current
	if err = checkStartup(bounded, home, s); err != nil {
		return "startup_unavailable"
	}
	running, err := startupRunning(bounded, s.Application)
	if err != nil {
		return "host_unavailable"
	}
	if !running {
		return "waiting_for_app"
	}
	ready, err := startupDesktopReady(bounded, s.Port)
	if err != nil || !ready || !startupListenerMatches(bounded, s.Application, s.Port) {
		return "cdp_unavailable"
	}
	return ""
}

func sameStartupApplication(saved, current string) bool {
	if saved == current {
		return true
	}
	if runtime.GOOS != "windows" {
		return false
	}
	if strings.EqualFold(saved, current) {
		return true
	}
	marker := `\windowsapps\openai.codex_`
	return strings.Contains(strings.ToLower(saved), marker) && strings.Contains(strings.ToLower(current), marker) && strings.EqualFold(filepath.Base(saved), filepath.Base(current))
}
