package codexruntime

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestSharedRegistrationControlsCancellationAndFailsClosed(t *testing.T) {
	profile := t.TempDir()
	file := filepath.Join(profile, RegistrationName)
	if got, err := Cancellation(profile); err != nil || got != "none" {
		t.Fatalf("legacy: %s %v", got, err)
	}
	valid := Registration{Version: 1, Profile: profile, Endpoint: "ws+unix://localhost/private/tmp/test.sock:/rpc"}
	write := func(value Registration) {
		t.Helper()
		body, _ := json.Marshal(value)
		if err := os.WriteFile(file, body, 0600); err != nil {
			t.Fatal(err)
		}
	}
	write(valid)
	if got, err := Cancellation(profile); err != nil || got != "session" {
		t.Fatalf("native: %s %v", got, err)
	}
	for _, endpoint := range []string{"ws://127.0.0.1:9000/rpc", "ws+unix://localhost/tmp/a%00.sock:/rpc", "ws+unix://localhost/tmp/a?x:/rpc"} {
		bad := valid
		bad.Endpoint = endpoint
		write(bad)
		if _, err := Cancellation(profile); err == nil {
			t.Fatalf("accepted endpoint %s", endpoint)
		}
	}
	bad := valid
	bad.Profile = t.TempDir()
	write(bad)
	if _, err := Cancellation(profile); err == nil {
		t.Fatal("accepted another profile")
	}
	write(valid)
	if runtime.GOOS != "windows" {
		if err := os.Chmod(file, 0644); err != nil {
			t.Fatal(err)
		}
		if _, err := Cancellation(profile); err == nil {
			t.Fatal("accepted public registration")
		}
	}
}
