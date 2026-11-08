package workflowmcp

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
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

func TestOversizedPublicationRetainsLocalRepairDiagnostic(t *testing.T) {
	var calls atomic.Int32
	client := harnessClient(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		t.Error("oversized request reached Core")
	})
	_, err := client.Publish(t.Context(), PublishInput{SessionID: "run", ExecutionID: "attempt", ExecutionHandle: "private-handle"}, map[string]any{
		"slot": "report", "seq": 1, "content_type": "text", "value": strings.Repeat("x", 32<<20),
	})
	var problem *coreapi.Error
	if calls.Load() != 0 || !errors.As(err, &problem) || problem.Code != "REQUEST_NOT_SENT" || !strings.Contains(problem.Message, "32 MiB") || problem.Fields["outcome"] != "not_sent" || problem.Fields["recovery"] == nil {
		t.Fatalf("local rejection became an uncertain write: calls=%d error=%+v", calls.Load(), err)
	}
	operation := problem.Fields["operation"].(map[string]any)
	if operation["session_id"] != "run" || operation["execution_id"] != "attempt" || operation["slot"] != "report" || operation["seq"] != 1 || operation["execution_handle"] != nil {
		t.Fatalf("wrong recovery identity: %+v", operation)
	}
}

func TestMutationRetainsUnknownOutcomeAfterResponseLoss(t *testing.T) {
	for _, failure := range []string{"disconnect", "decode"} {
		t.Run(failure, func(t *testing.T) {
			var writes atomic.Int32
			client := harnessClient(t, func(w http.ResponseWriter, r *http.Request) {
				if _, err := io.Copy(io.Discard, r.Body); err != nil {
					t.Error(err)
				}
				writes.Add(1)
				if failure == "decode" {
					_, _ = w.Write([]byte(`{"ok":true,"result":{"execution_id":123}}`))
					return
				}
				connection, _, err := w.(http.Hijacker).Hijack()
				if err != nil {
					t.Error(err)
					return
				}
				_ = connection.Close()
			})
			result := inputToolCall(t, inputToolSession(t, client), "workflow.step.complete", map[string]any{
				"session_id": "run", "execution_id": "attempt", "execution_handle": "private-handle", "outcome": "succeeded",
			})
			raw, _ := json.Marshal(result.StructuredContent)
			var value struct {
				Error map[string]any `json:"error"`
			}
			if err := json.Unmarshal(raw, &value); err != nil {
				t.Fatal(err)
			}
			if !result.IsError || writes.Load() != 1 || value.Error["code"] != "REQUEST_OUTCOME_UNKNOWN" || value.Error["outcome"] != "unknown" || value.Error["recovery"] == nil || strings.Contains(string(raw), "private-handle") {
				t.Fatalf("uncertain write was misclassified or repeated: writes=%d result=%s", writes.Load(), raw)
			}
			operation := value.Error["operation"].(map[string]any)
			if operation["session_id"] != "run" || operation["execution_id"] != "attempt" {
				t.Fatalf("lost recovery identity: %s", raw)
			}
		})
	}
}

func TestPartialToolCatalogWarningsReachMCP(t *testing.T) {
	client := harnessClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true,"result":{"tools":[{"name":"knowledge_list_documents","available":true}],"warnings":[{"source":"package","error":{"code":"CAPABILITY_UNAVAILABLE","message":"Package discovery offline"}}]}}`))
	})
	result := inputToolCall(t, inputToolSession(t, client), "workflow.tool.list", map[string]any{"session_id": "run", "execution_id": "attempt", "execution_handle": "handle"})
	raw, _ := json.Marshal(result.StructuredContent)
	var value struct {
		Tools    []map[string]any `json:"tools"`
		Warnings []map[string]any `json:"warnings"`
	}
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatal(err)
	}
	if result.IsError || len(value.Tools) != 1 || value.Tools[0]["name"] != "knowledge_list_documents" || len(value.Warnings) != 1 || value.Warnings[0]["source"] != "package" {
		t.Fatalf("partial catalog lost: %s", raw)
	}
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
