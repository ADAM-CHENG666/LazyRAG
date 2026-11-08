package workflowmcp

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"lazymind/agentconnector/internal/coreapi"
	"lazymind/agentconnector/internal/credentials"
)

func harnessClient(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	store, err := credentials.NewStore(t.TempDir(), "")
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Save(credentials.Credentials{ServerURL: server.URL, AccessToken: "test", RefreshToken: "refresh"}); err != nil {
		t.Fatal(err)
	}
	api, err := coreapi.New(store)
	if err != nil {
		t.Fatal(err)
	}
	return &Client{api: api}
}

func TestCoreDiagnosticsReachMCPWithReceiptAndFutureFields(t *testing.T) {
	client := harnessClient(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/projection") {
			_, _ = w.Write([]byte(`{"session_id":"run","state_version":4}`))
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/executions:begin") {
			t.Errorf("unexpected fallback: %s", r.URL.Path)
		}
		w.WriteHeader(409)
		_, _ = w.Write([]byte(`{"ok":false,"error":{"code":"STEP_NOT_READY","message":"Missing source","retryable":false,"details":{"missing_groups":["source"]},"recovery":"Supply the source","future_diagnostic":{"v":2}},"result":{"accepted":false,"state_version":4,"projection":{"ready":["collect"]}}}`))
	})
	result := inputToolCall(t, inputToolSession(t, client), "workflow.step.begin", map[string]any{"session_id": "run", "step_id": "write", "command_id": "same-command"})
	if !result.IsError {
		t.Fatal("rejection became success")
	}
	raw, _ := json.Marshal(result.StructuredContent)
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	problem := decoded["error"].(map[string]any)
	if problem["code"] != "STEP_NOT_READY" || problem["details"] == nil || problem["recovery"] != "Supply the source" || problem["future_diagnostic"] == nil || decoded["result"] == nil {
		t.Fatalf("diagnostics lost: %s", raw)
	}
}

func TestCommittedWritesSurviveProjectionOutageOnMCPWire(t *testing.T) {
	for _, name := range []string{"workflow.start", "workflow.step.claim", "workflow.step.complete"} {
		t.Run(name, func(t *testing.T) {
			writes := 0
			client := harnessClient(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/projection") {
					w.WriteHeader(503)
					_, _ = w.Write([]byte(`{"ok":false,"error":{"code":"STATE_UNAVAILABLE","message":"offline"}}`))
					return
				}
				writes++
				var response any
				switch {
				case strings.HasSuffix(r.URL.Path, "/workflow-preparations"):
					response = map[string]any{"status": "ready", "preparation_id": "prep"}
				case strings.HasSuffix(r.URL.Path, ":consume"):
					response = map[string]any{"session_id": "run", "workflow_id": "wf", "workflow_revision_id": "revision"}
				case strings.HasSuffix(r.URL.Path, ":begin"):
					response = map[string]any{"execution_id": "attempt", "execution_handle": "handle", "step_contract": map[string]any{"prompt": "Write"}}
				case strings.HasSuffix(r.URL.Path, ":complete"):
					response = map[string]any{"execution_id": "attempt", "attempt_status": "succeeded", "receipt": map[string]any{"execution_id": "attempt"}, "control": map[string]any{"continuation": "awaiting_user"}}
				default:
					t.Errorf("unexpected path: %s", r.URL.Path)
				}
				_ = json.NewEncoder(w).Encode(response)
			})
			args := map[string]any{"session_id": "run", "execution_id": "attempt"}
			if name == "workflow.start" {
				args = map[string]any{"workflow_id": "wf", "idempotency_key": "stable-start-key-123"}
			}
			if name == "workflow.step.complete" {
				args["execution_handle"], args["outcome"] = "handle", "succeeded"
			}
			result := inputToolCall(t, inputToolSession(t, client), name, args)
			raw, _ := json.Marshal(result.StructuredContent)
			var value map[string]any
			_ = json.Unmarshal(raw, &value)
			if result.IsError || value["state_unavailable"] != true || value["warning"] == nil {
				t.Fatalf("successful write lost: %s", raw)
			}
			if name == "workflow.step.claim" && value["execution"].(map[string]any)["execution_handle"] != "handle" {
				t.Fatalf("claim lost: %s", raw)
			}
			if name == "workflow.start" && (value["session_id"] != "run" || value["idempotency_key"] != "stable-start-key-123") {
				t.Fatalf("start lost: %s", raw)
			}
			wantWrites := 1
			if name == "workflow.start" {
				wantWrites = 2
			}
			if writes != wantWrites {
				t.Fatalf("duplicated mutation: %d", writes)
			}
		})
	}
}

func TestBeginKeepsCreatedExecutionWhenClaimResponseIsLost(t *testing.T) {
	client := harnessClient(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/projection"):
			_, _ = w.Write([]byte(`{"session_id":"run","state_version":4}`))
		case strings.HasSuffix(r.URL.Path, "/executions:begin"):
			_, _ = w.Write([]byte(`{"receipt":{"execution_id":"created","command_id":"original"}}`))
		default:
			w.WriteHeader(503)
			_, _ = w.Write([]byte(`{"ok":false,"error":{"code":"UNAVAILABLE","message":"response lost"}}`))
		}
	})
	result, err := client.Begin(t.Context(), BeginInput{SessionID: "run", StepID: "write", CommandID: "original"})
	if err != nil || result.Execution.ExecutionID != "created" || !result.ClaimRequired || result.Receipt["command_id"] != "original" || result.Warning["outcome"] != "unknown" {
		t.Fatalf("lost accepted begin: %+v %v", result, err)
	}
}
