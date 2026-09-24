package mcpbridge

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"lazymind/agentconnector/internal/coreapi"
	"lazymind/agentconnector/internal/credentials"
	"lazymind/agentconnector/internal/workflowhost"
)

func TestCodexBindingUsesCurrentAccountAndProfilePairing(t *testing.T) {
	home, profile := t.TempDir(), t.TempDir()
	calls := 0
	var received map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPost || r.URL.Path != "/api/core/workflow-sessions/run-1/host-binding" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&received)
		_, _ = w.Write([]byte(`{"control":{}}`))
	}))
	defer server.Close()
	store, _ := credentials.NewStore(home, "")
	if err := store.Save(credentials.Credentials{ServerURL: server.URL, AccessToken: "access", RefreshToken: "refresh"}); err != nil {
		t.Fatal(err)
	}
	account, err := store.AccountScope()
	if err != nil {
		t.Fatal(err)
	}
	pair, err := workflowhost.EnsureForProvider(home, "codex", profile, account)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CODEX_HOME", profile)
	t.Setenv("LAZYMIND_WORKFLOW_PAIRING_FILE", filepath.Join(home, "workflow-hosts", pair.ConnectorID+".json"))
	api, _ := coreapi.New(store)
	bridge := &Bridge{home: home, api: api}
	if err := bridge.bindCodexController(context.Background(), "run-1", "thread-1"); err != nil {
		t.Fatal(err)
	}
	if received["provider"] != "codex" || received["driver_session_id"] != "thread-1" || received["connector_id"] != pair.ConnectorID || received["credential"] != pair.Token {
		t.Fatal("binding did not use paired identity")
	}
	t.Setenv("CODEX_HOME", t.TempDir())
	if err := bridge.bindCodexController(context.Background(), "run-1", "thread-1"); err == nil {
		t.Fatal("accepted wrong profile")
	}
	t.Setenv("CODEX_HOME", profile)
	if err := workflowhost.Disable(home, pair.ConnectorID); err != nil {
		t.Fatal(err)
	}
	if err := bridge.bindCodexController(context.Background(), "run-1", "thread-1"); err == nil {
		t.Fatal("accepted disabled pairing")
	}
	if calls != 1 {
		t.Fatalf("unexpected bind calls: %d", calls)
	}
}
