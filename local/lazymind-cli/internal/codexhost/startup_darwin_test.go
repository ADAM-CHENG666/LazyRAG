package codexhost

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDockWriteScriptUsesNativePreferenceAPI(t *testing.T) {
	// Exercise JXA itself in an isolated domain, never the user's Dock.
	domain := fmt.Sprintf("com.lazymind.test-dock-%d-%d", os.Getpid(), time.Now().UnixNano())
	script := strings.Replace(writeDockScript, "com.apple.dock", domain, 1)
	t.Cleanup(func() {
		cleanup := fmt.Sprintf(`ObjC.import('Foundation'); const d = $.NSUserDefaults.standardUserDefaults; d.removePersistentDomainForName('%s'); d.synchronize;`, domain)
		if out, err := exec.Command("/usr/bin/osascript", "-l", "JavaScript", "-e", cleanup).CombinedOutput(); err != nil {
			t.Errorf("remove isolated preferences: %s %v", out, err)
		}
	})
	if out, err := exec.Command("/usr/bin/osascript", "-l", "JavaScript", "-e", script, `[{"GUID":123,"tile-type":"file-tile"}]`).CombinedOutput(); err != nil {
		t.Fatalf("write isolated preferences: %s %v", out, err)
	}
	read := fmt.Sprintf(`ObjC.import('Foundation'); JSON.stringify(ObjC.deepUnwrap($.NSUserDefaults.alloc.initWithSuiteName('%s').objectForKey('persistent-apps')))`, domain)
	out, err := exec.Command("/usr/bin/osascript", "-l", "JavaScript", "-e", read).CombinedOutput()
	var entries []dockEntry
	if err != nil || json.Unmarshal(out, &entries) != nil || len(entries) != 1 || dockID(entries[0]) != 123 {
		t.Fatalf("read isolated preferences: %s %v", out, err)
	}
}

func fakeDock(t *testing.T, entries *[]dockEntry) {
	t.Helper()
	previous := startupRun
	startupRun = func(ctx context.Context, name string, args ...string) ([]byte, error) {
		if name == "/usr/bin/killall" {
			return nil, nil
		}
		if name != "/usr/bin/osascript" {
			return nil, fmt.Errorf("unexpected command %s", name)
		}
		if len(args) == 4 {
			return json.Marshal(*entries)
		}
		if len(args) != 5 {
			return nil, fmt.Errorf("unexpected arguments %v", args)
		}
		return nil, json.Unmarshal([]byte(args[4]), entries)
	}
	t.Cleanup(func() { startupRun = previous })
}
func TestMacStartupPersistsAndRestoresOnlyOwnedDockEntries(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "state")
	application := filepath.Join(root, "A ' quoted $ app.app")
	oldHome := startupUserHome
	startupUserHome = func() (string, error) { return root, nil }
	t.Cleanup(func() { startupUserHome = oldHome })
	original := launcherDockEntry(11, application)
	original["unrelated-field"] = "keep"
	entries := []dockEntry{original, launcherDockEntry(12, "/Applications/Other.app")}
	fakeDock(t, &entries)
	s := startupRecord{Application: application, Port: 9334}
	for i := 0; i < 2; i++ {
		if err := configureStartup(context.Background(), home, &s, application); err != nil {
			t.Fatal(err)
		}
	}
	if len(entries) != 2 || len(s.Dock) != 1 || dockPath(entries[0]) != s.Launcher {
		t.Fatalf("duplicate or missing entry: %+v", s)
	}
	if dockID(entries[0]) == 11 || dockID(entries[0]) == 12 {
		t.Fatal("replacement reused an existing Dock tile identity")
	}
	if err := checkStartup(context.Background(), home, s); err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(s.Launcher, "Contents", "MacOS", "launch")
	if out, err := exec.Command("/bin/sh", "-n", script).CombinedOutput(); err != nil {
		t.Fatalf("invalid launcher: %s %v", out, err)
	}
	if out, err := exec.Command("/usr/bin/plutil", "-lint", filepath.Join(s.Launcher, "Contents", "Info.plist")).CombinedOutput(); err != nil {
		t.Fatalf("invalid bundle: %s %v", out, err)
	}
	body, _ := os.ReadFile(script)
	if !strings.Contains(string(body), "--remote-debugging-port=9334") {
		t.Fatal("missing port")
	}
	entries[1]["user-change"] = "preserve"
	if err := disableStartup(context.Background(), home, s); err != nil {
		t.Fatal(err)
	}
	if dockID(entries[0]) != 11 || dockPath(entries[0]) != application || entries[0]["unrelated-field"] != "keep" || entries[1]["user-change"] != "preserve" {
		t.Fatalf("Dock restore: %+v", entries)
	}
	if _, err := os.Stat(s.Launcher); !os.IsNotExist(err) {
		t.Fatal("owned launcher not removed")
	}
}

func TestMacStartupDetectsRemovedDockEntryAndPreservesUserFiles(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "state")
	oldHome := startupUserHome
	startupUserHome = func() (string, error) { return root, nil }
	t.Cleanup(func() { startupUserHome = oldHome })
	entries := []dockEntry{}
	fakeDock(t, &entries)
	s := startupRecord{Application: "/Applications/ChatGPT.app", Port: 9334}
	if err := configureStartup(context.Background(), home, &s, ""); err != nil {
		t.Fatal(err)
	}
	entries = nil
	if err := checkStartup(context.Background(), home, s); err == nil {
		t.Fatal("missing Dock launcher passed")
	}
	userFile := filepath.Join(s.Launcher, "user-note")
	_ = os.WriteFile(userFile, []byte("keep"), 0600)
	if err := disableStartup(context.Background(), home, s); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(userFile); err != nil {
		t.Fatal("deleted unrelated file")
	}
}

func TestStartupHealthDistinguishesWaitingFromMissingControlAndForeignListener(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "state")
	oldHome := startupUserHome
	startupUserHome = func() (string, error) { return root, nil }
	t.Cleanup(func() { startupUserHome = oldHome })
	entries := []dockEntry{}
	fakeDock(t, &entries)
	s := startupRecord{Application: "/Applications/ChatGPT.app", Port: 9334}
	if err := configureStartup(context.Background(), home, &s, ""); err != nil {
		t.Fatal(err)
	}
	oldApp, oldRunning, oldReady, oldListener := startupApplication, startupRunning, startupDesktopReady, startupListenerMatches
	t.Cleanup(func() {
		startupApplication, startupRunning, startupDesktopReady, startupListenerMatches = oldApp, oldRunning, oldReady, oldListener
	})
	startupApplication = func() (string, error) { return s.Application, nil }
	for _, tc := range []struct {
		name                  string
		running, ready, owned bool
		want                  string
	}{
		{"closed", false, false, false, "waiting_for_app"},
		{"running without CDP", true, false, false, "cdp_unavailable"},
		{"wrong listener", true, true, false, "cdp_unavailable"},
		{"connected", true, true, true, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			startupRunning = func(context.Context, string) (bool, error) { return tc.running, nil }
			startupDesktopReady = func(context.Context, int) (bool, error) { return tc.ready, nil }
			startupListenerMatches = func(context.Context, string, int) bool { return tc.owned }
			before, _ := os.ReadFile(startupPath(home))
			if got := StartupHealth(context.Background(), home); got != tc.want {
				t.Fatalf("got %q want %q", got, tc.want)
			}
			after, _ := os.ReadFile(startupPath(home))
			if string(before) != string(after) {
				t.Fatal("health check changed configuration")
			}
		})
	}
	entries = nil
	if got := StartupHealth(context.Background(), home); got != "startup_unavailable" {
		t.Fatalf("lost launcher: %s", got)
	}
}
