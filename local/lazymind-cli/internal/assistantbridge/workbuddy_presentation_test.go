package assistantbridge

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"lazymind/agentconnector/internal/credentials"
	"lazymind/agentconnector/internal/workflowhost"
)

func TestWorkBuddyPresentationUsesOnlyPairedBindingsAndCreationOrder(t *testing.T) {
	var connector string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			t.Errorf("presentation mutated Core: %s", r.Method)
		}
		if r.URL.Path == "/api/core/workflow-sessions" {
			if r.URL.Query().Get("page_token") == "next" {
				_, _ = w.Write([]byte(`{"sessions":[{"session_id":"new","created_at":"2026-10-19T02:00:00Z"}]}`))
			} else {
				_, _ = w.Write([]byte(`{"sessions":[
				{"session_id":"old","created_at":"2026-10-19T01:00:00Z","updated_at":"2026-10-20T01:00:00Z"},
				{"session_id":"foreign","created_at":"2026-10-19T04:00:00Z"},
				{"session_id":"failed","created_at":"2026-10-19T03:00:00Z"},
				{"session_id":"other-provider","created_at":"2026-10-19T05:00:00Z"},
				{"session_id":"other-session","created_at":"2026-10-19T06:00:00Z"}],"next_page_token":"next"}`))
			}
			return
		}
		id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/core/workflow-sessions/"), "/control")
		binding := map[string]any{"bound": true, "provider": "workbuddy", "connector_id": connector, "driver_session_id": "native-1"}
		switch id {
		case "foreign":
			binding["connector_id"] = "another-connector"
		case "failed":
			binding["bound"] = false
		case "other-provider":
			binding["provider"] = "codex"
		case "other-session":
			binding["driver_session_id"] = "native-2"
		case "old", "new":
		default:
			t.Errorf("unexpected run %s", id)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"control": map[string]any{"binding": binding}})
	}))
	defer upstream.Close()
	home := t.TempDir()
	server := newTestServer(t, home)
	if err := server.store.Save(credentials.Credentials{ServerURL: upstream.URL, AccessToken: "access", RefreshToken: "refresh", SavedAt: float64(time.Now().Unix()), ExpiresIn: 3600}); err != nil {
		t.Fatal(err)
	}
	account, err := server.store.AccountScope()
	if err != nil {
		t.Fatal(err)
	}
	pair, err := workflowhost.EnsureForProvider(home, "workbuddy", filepath.Join(home, "profile"), account)
	if err != nil {
		t.Fatal(err)
	}
	connector = pair.ConnectorID
	for _, test := range []struct {
		session, token, run string
		code                int
	}{
		{"native-1", pair.Token, "new", 200},
		{"unbound", pair.Token, "", 200},
		{"native-1", "invalid", "", 403},
		{"../bad", pair.Token, "", 422},
	} {
		r := httptest.NewRequest("GET", "/v1/workbuddy/presentation?session_id="+test.session, nil)
		r.Header.Set("Authorization", "Bearer "+test.token)
		r.Header.Set("X-LazyMind-Connector-Id", pair.ConnectorID)
		w := httptest.NewRecorder()
		server.routes().ServeHTTP(w, r)
		if w.Code != test.code {
			t.Fatalf("status %d: %s", w.Code, w.Body.String())
		}
		if w.Code == 200 {
			var result map[string]string
			if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
				t.Fatal(err)
			}
			if result["run_id"] != test.run || result["session_id"] != test.session {
				t.Fatalf("wrong target: %v", result)
			}
			if len(result) != 2 {
				t.Fatal("presentation exposed unnecessary data")
			}
		}
	}
}
