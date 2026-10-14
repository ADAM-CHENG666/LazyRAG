package agentexec

import (
	"os"
	"path/filepath"
	"testing"
)

func TestResolveDSHDesktopCLI(t *testing.T) {
	app := filepath.Join(t.TempDir(), "应用 space.app")
	resources := filepath.Join(app, "Contents", "Resources")
	cli := filepath.Join(resources, "runtime", "cli", "bin", "dsh")
	if err := os.MkdirAll(filepath.Dir(cli), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cli, []byte("#!/bin/sh\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(resources, "app.asar"), []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	want, _ := filepath.EvalSymlinks(cli)
	for _, value := range []string{app, cli} {
		got, err := ResolveDSHDesktopCLI(value)
		if err != nil || got != want {
			t.Fatalf("resolve %s: %s %v", value, got, err)
		}
	}
	link := filepath.Join(t.TempDir(), "dsh")
	if err := os.Symlink(cli, link); err != nil {
		t.Fatal(err)
	}
	if got, err := ResolveDSHDesktopCLI(link); err != nil || got != want {
		t.Fatalf("symlink: %s %v", got, err)
	}
	t.Setenv("LAZYMIND_DSH_DESKTOP_PATH", app)
	t.Setenv("LAZYMIND_DSH_PATH", "/old/npm/dsh")
	if got, err := FindDSHDesktopCLI(); err != nil || got != want {
		t.Fatalf("explicit Desktop: %s %v", got, err)
	}
	if err := os.Remove(filepath.Join(resources, "app.asar")); err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveDSHDesktopCLI(cli); err == nil {
		t.Fatal("incomplete installation accepted")
	}
	if _, err := ResolveDSHDesktopCLI("/bin/sh"); err == nil {
		t.Fatal("ordinary executable accepted as Desktop")
	}
}
