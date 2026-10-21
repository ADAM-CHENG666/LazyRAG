package workbuddyhost

import (
	"context"
	"encoding/json"
	"errors"
	"lazymind/agentconnector/internal/workflowhost"
	"path/filepath"
	"testing"
	"time"
)

func mcpStatusFixture(t *testing.T) (string, workflowhost.Pairing) {
	t.Helper()
	home := t.TempDir()
	pair, err := workflowhost.EnsureForProvider(home, "workbuddy", filepath.Join(home, "profile"), "account")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(Config{PairingFile: filepath.Join(home, "workflow-hosts", pair.ConnectorID+".json"), Node: filepath.Join(home, "node"), CDPPort: 9333})
	if err = writeAtomic(ConfigPath(home), body); err != nil {
		t.Fatal(err)
	}
	return home, pair
}
func TestMCPReadyRequiresFreshMatchingSession(t *testing.T) {
	home, pair := mcpStatusFixture(t)
	if MCPReady(home, "account") {
		t.Fatal("installation alone must not count as MCP readiness")
	}
	for _, tc := range []struct {
		name            string
		age             time.Duration
		connector, hash string
		want            bool
	}{
		{"connected", 0, pair.ConnectorID, pairingHash(pair.Token), true},
		{"expired", 10 * time.Second, pair.ConnectorID, pairingHash(pair.Token), false},
		{"future", -10 * time.Second, pair.ConnectorID, pairingHash(pair.Token), false},
		{"other pairing", 0, "other", pairingHash(pair.Token), false},
		{"rotated token", 0, pair.ConnectorID, "old", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body, _ := json.Marshal(mcpSessionStatus{tc.connector, tc.hash, time.Now().Add(-tc.age).UnixMilli()})
			if err := writeAtomic(filepath.Join(home, "workbuddy-workflow", "mcp-sessions", "test.json"), body); err != nil {
				t.Fatal(err)
			}
			if got := MCPReady(home, "account"); got != tc.want {
				t.Fatalf("ready=%v want=%v", got, tc.want)
			}
			if MCPReady(home, "other-account") {
				t.Fatal("cross-account receipt accepted")
			}
		})
	}
}
func TestMCPSessionReportsOnlyAfterPingAndRemovesOnExit(t *testing.T) {
	for _, ok := range []bool{true, false} {
		t.Run(map[bool]string{true: "connected", false: "unresponsive"}[ok], func(t *testing.T) {
			home, _ := mcpStatusFixture(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			entered := make(chan struct{})
			release := make(chan struct{})
			done := make(chan struct{})
			go func() {
				defer close(done)
				ReportMCPSession(ctx, home, "account", "instance", func(context.Context) error {
					close(entered)
					<-release
					if !ok {
						return errors.New("no client")
					}
					return nil
				})
			}()
			<-entered
			if MCPReady(home, "account") {
				t.Fatal("reported before ping")
			}
			close(release)
			if ok {
				deadline := time.Now().Add(time.Second)
				for !MCPReady(home, "account") && time.Now().Before(deadline) {
					time.Sleep(time.Millisecond)
				}
				if !MCPReady(home, "account") {
					t.Fatal("successful session not reported")
				}
			}
			cancel()
			<-done
			if MCPReady(home, "account") {
				t.Fatal("receipt survived disconnect")
			}
			files, _ := filepath.Glob(filepath.Join(home, "workbuddy-workflow", "mcp-sessions", "*.json"))
			if len(files) > 0 {
				t.Fatal("session file remains")
			}
		})
	}
}
