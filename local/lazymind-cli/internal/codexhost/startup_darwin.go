package codexhost

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"lazymind/agentconnector/internal/desktopsetup"
)

var startupUserHome = os.UserHomeDir
var startupRun = func(ctx context.Context, name string, args ...string) ([]byte, error) {
	output, err := exec.CommandContext(ctx, name, args...).CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("%s: %s: %w", name, output, err)
	}
	return output, nil
}

type dockEntry map[string]any

func readDock(ctx context.Context) ([]dockEntry, error) {
	body, err := startupRun(ctx, "/usr/bin/osascript", "-l", "JavaScript", "-e", `ObjC.import('Foundation'); JSON.stringify(ObjC.deepUnwrap($.NSUserDefaults.alloc.initWithSuiteName('com.apple.dock').objectForKey('persistent-apps')) || [])`)
	if err != nil {
		return nil, err
	}
	var entries []dockEntry
	if json.Unmarshal(body, &entries) != nil {
		return nil, fmt.Errorf("cannot read Dock entries")
	}
	return entries, nil
}

const writeDockScript = `ObjC.import('Foundation'); function run(args) { const d = $.NSUserDefaults.alloc.initWithSuiteName('com.apple.dock'); d.setObjectForKey($(JSON.parse(args[0])), 'persistent-apps'); if (!d.synchronize) throw Error('Cannot save Dock entries'); }`

func writeDock(ctx context.Context, entries []dockEntry) error {
	body, _ := json.Marshal(entries)
	_, err := startupRun(ctx, "/usr/bin/osascript", "-l", "JavaScript", "-e", writeDockScript, string(body))
	if err != nil {
		return err
	}
	// Reload the Dock only during the explicit installation/disable operation.
	_, err = startupRun(ctx, "/usr/bin/killall", "Dock")
	return err
}
func dockPath(entry dockEntry) string {
	data, _ := entry["tile-data"].(map[string]any)
	file, _ := data["file-data"].(map[string]any)
	raw, _ := file["_CFURLString"].(string)
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return strings.TrimSuffix(u.Path, "/")
}
func dockID(entry dockEntry) int64 { value, _ := entry["GUID"].(float64); return int64(value) }
func launcherDockEntry(id int64, launcher string) dockEntry {
	u := url.URL{Scheme: "file", Path: launcher + "/"}
	return dockEntry{"GUID": float64(id), "tile-type": "file-tile", "tile-data": map[string]any{
		"file-data":  map[string]any{"_CFURLString": u.String(), "_CFURLStringType": 15},
		"file-label": "Codex (LazyMind)", "file-type": 41, "bundle-identifier": "com.lazymind.codex-launcher"}}
}

func freshDockID(entries []dockEntry) int64 {
	id := int64(uint32(time.Now().UnixNano()))
	used := map[int64]bool{}
	for _, entry := range entries {
		used[dockID(entry)] = true
	}
	for id == 0 || used[id] {
		id = int64(uint32(id + 1))
	}
	return id
}

func configureStartup(ctx context.Context, home string, s *startupRecord, previous string) error {
	user, err := startupUserHome()
	if err != nil {
		return err
	}
	if s.Launcher == "" {
		s.Launcher = filepath.Join(user, "Applications", "Codex (LazyMind).app")
	}
	files, err := desktopsetup.Load(filepath.Join(home, "codex-workflow", "startup-files.json"))
	if err != nil {
		return err
	}
	if _, err = os.Stat(s.Launcher); err == nil && len(files.Entries) == 0 {
		return fmt.Errorf("Codex launcher already exists outside this LazyMind installation")
	}
	entries, err := readDock(ctx)
	if err != nil {
		return err
	}
	found := false
	for i, entry := range entries {
		path := dockPath(entry)
		if path == s.Launcher {
			found = true
			continue
		}
		if path != s.Application && (previous == "" || path != previous) {
			continue
		}
		// Give the replacement its own tile identity, and preserve the original
		// identity only in the restoration record.
		id := freshDockID(entries)
		body, _ := json.Marshal(entry)
		s.Dock = append(s.Dock, dockChange{ID: id, Before: body})
		entries[i] = launcherDockEntry(id, s.Launcher)
		found = true
	}
	if !found {
		id := freshDockID(entries)
		entries = append(entries, launcherDockEntry(id, s.Launcher))
		s.Dock = append(s.Dock, dockChange{ID: id})
	}
	if err = saveStartup(home, *s); err != nil {
		return err
	}
	plist := `<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.lazymind.codex-launcher</string><key>CFBundleName</key><string>Codex (LazyMind)</string><key>CFBundleExecutable</key><string>launch</string><key>CFBundlePackageType</key><string>APPL</string><key>LSUIElement</key><true/></dict></plist>`
	if icon := desktopIcon(ctx, s.Application); len(icon) > 0 {
		if err = files.Put(filepath.Join(s.Launcher, "Contents", "Resources", "codex.icns"), icon, 0600); err != nil {
			return err
		}
		plist = strings.Replace(plist, "</dict></plist>", "<key>CFBundleIconFile</key><string>codex.icns</string></dict></plist>", 1)
	}
	if err = files.Put(filepath.Join(s.Launcher, "Contents", "Info.plist"), []byte(plist), 0600); err != nil {
		return err
	}
	choice := desktopsetup.ShellQuote(filepath.Join(home, "codex-workflow", "enabled.json"))
	script := "#!/bin/sh\nif [ -f " + choice + " ] && [ \"$(cat " + choice + ")\" != true ]; then exec /usr/bin/open -a " + desktopsetup.ShellQuote(s.Application) + "; fi\nexec /usr/bin/open -a " + desktopsetup.ShellQuote(s.Application) + " --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=" + strconv.Itoa(s.Port) + "\n"
	if err = files.Put(filepath.Join(s.Launcher, "Contents", "MacOS", "launch"), []byte(script), 0700); err != nil {
		return err
	}
	// An earlier attempt may have persisted preferences but failed before Dock
	// reloaded them. Explicit installation must finish publication on retries.
	return writeDock(ctx, entries)
}

func disableStartup(ctx context.Context, home string, s startupRecord) error {
	entries, err := readDock(ctx)
	if err != nil {
		return err
	}
	result := make([]dockEntry, 0, len(entries))
	changed := false
	for _, entry := range entries {
		if dockPath(entry) != s.Launcher {
			result = append(result, entry)
			continue
		}
		// Only restore entries still pointing at our launcher. A user move is preserved.
		changed = true
		for _, old := range s.Dock {
			if old.ID == dockID(entry) && len(old.Before) > 0 {
				var original dockEntry
				if json.Unmarshal(old.Before, &original) != nil {
					return fmt.Errorf("invalid saved Dock entry")
				}
				result = append(result, original)
				break
			}
		}
	}
	if changed {
		if err = writeDock(ctx, result); err != nil {
			return err
		}
	}
	files, err := desktopsetup.Load(filepath.Join(home, "codex-workflow", "startup-files.json"))
	if err != nil {
		return err
	}
	if err = files.Restore(); err != nil {
		return err
	}
	// Remove empty owned directories only; never recursively delete user content.
	_ = os.Remove(filepath.Join(s.Launcher, "Contents", "MacOS"))
	_ = os.Remove(filepath.Join(s.Launcher, "Contents", "Resources"))
	_ = os.Remove(filepath.Join(s.Launcher, "Contents"))
	_ = os.Remove(s.Launcher)
	return nil
}

func desktopIcon(ctx context.Context, application string) []byte {
	info := filepath.Join(application, "Contents", "Info.plist")
	if _, err := os.Stat(info); err != nil {
		return nil
	}
	output, err := exec.CommandContext(ctx, "/usr/bin/plutil", "-extract", "CFBundleIconFile", "raw", "-o", "-", info).Output()
	if err != nil {
		return nil
	}
	name := strings.TrimSpace(string(output))
	if name == "" || filepath.Base(name) != name {
		return nil
	}
	if filepath.Ext(name) == "" {
		name += ".icns"
	}
	body, _ := os.ReadFile(filepath.Join(application, "Contents", "Resources", name))
	return body
}
func checkStartup(ctx context.Context, home string, s startupRecord) error {
	files, err := desktopsetup.Load(filepath.Join(home, "codex-workflow", "startup-files.json"))
	if err != nil {
		return err
	}
	if err = files.Check(); err != nil {
		return err
	}
	entries, err := readDock(ctx)
	if err != nil {
		return err
	}
	found := false
	for _, entry := range entries {
		if dockPath(entry) == s.Application {
			return fmt.Errorf("Codex Dock entry bypasses startup configuration")
		}
		if dockPath(entry) == s.Launcher {
			found = true
		}
	}
	if found {
		return nil
	}
	return fmt.Errorf("Codex Dock launcher is missing")
}
