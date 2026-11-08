package externalcapability

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"lazymind/core/capability"
	"lazymind/core/common/orm"
)

func TestSharedFilesCheckOwnershipIntegrityAndExplicitInput(t *testing.T) {
	db := orm.MigrateTestDB(t, &orm.WorkflowInputResource{})
	service := New(db.DB, nil)
	content := []byte("owned document")
	resource := orm.WorkflowInputResource{ID: "resource-1", OwnerUserID: "owner", Name: "document.txt",
		Content: content, Size: int64(len(content)), ContentHash: fmt.Sprintf("sha256:%x", sha256.Sum256(content)), Revision: 1}
	if err := db.Create(&resource).Error; err != nil {
		t.Fatal(err)
	}
	input := map[string]any{"source": map[string]any{"resource_id": resource.ID}}
	resolved, err := service.resolveSharedFiles(t.Context(), "owner", "resource.read", input)
	if err != nil {
		t.Fatal(err)
	}
	file := resolved["source"].(map[string]any)
	if file["content_base64"] != base64.StdEncoding.EncodeToString(content) || file["resource_id"] != nil {
		t.Fatalf("unexpected resolved file: %#v", file)
	}
	if input["source"].(map[string]any)["resource_id"] != resource.ID {
		t.Fatal("caller input mutated")
	}
	if _, err := service.resolveSharedFiles(t.Context(), "foreign", "resource.read", input); err == nil {
		t.Fatal("foreign resource was accepted")
	}
	for _, source := range []map[string]any{
		{"local_path": "/etc/passwd"},
		{"resource_id": resource.ID, "content_base64": "YQ=="},
		{"resource_id": resource.ID, "sha256": strings.Repeat("0", 64)},
	} {
		if _, err := service.resolveSharedFiles(t.Context(), "owner", "resource.read", map[string]any{"source": source}); err == nil {
			t.Fatalf("invalid source accepted: %#v", source)
		}
	}
	if err := db.Model(&resource).Update("content_hash", "sha256:broken").Error; err != nil {
		t.Fatal(err)
	}
	if _, err := service.resolveSharedFiles(t.Context(), "owner", "resource.read", input); err == nil {
		t.Fatal("corrupt resource accepted")
	}
}

func TestSharedToolInvocationPreservesRecoveryAndAcceptsFileSizedResults(t *testing.T) {
	db := testDB(t)
	fail := true
	payload := strings.Repeat("x", 3<<20)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if models, ok := request["llm_config"].(map[string]any); !ok || len(models) != 0 {
			t.Fatalf("deterministic tool received model configuration: %#v", request["llm_config"])
		}
		if r.URL.Path == "/api/chat/tools/external-catalog" {
			_, _ = w.Write([]byte(`{"items":[{"name":"document.patch.apply","available":true,"input_schema":{"type":"object"}}]}`))
			return
		}
		if fail {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"detail":{"code":"VERSION_CONFLICT","message":"Document changed.","retryable":false,"details":{"actual_sha256":"current"},"recovery":"Read the current document and recompute the patch."}}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"result": map[string]any{"content": payload}})
	}))
	defer server.Close()
	t.Setenv("LAZYMIND_CHAT_SERVICE_URL", server.URL)
	service := New(db, server.Client())
	call := capability.InvocationContext{Principal: capability.Principal{UserID: "user-1"}, ExternalAgent: "codex"}
	input := capability.InvokeExternalToolInput{ToolID: "builtin:document.patch.apply", Arguments: map[string]any{}}
	_, err := service.InvokeExternalTool(context.Background(), call, input)
	var failure *capability.Error
	if !errors.As(err, &failure) || failure.Code != capability.Conflict || failure.Details["actual_sha256"] != "current" || !strings.Contains(failure.Reason, "recompute") {
		t.Fatalf("lost recovery diagnostics: %#v / %v", failure, err)
	}
	fail = false
	result, err := service.InvokeExternalTool(context.Background(), call, input)
	if err != nil {
		t.Fatal(err)
	}
	if result.Result.(map[string]any)["content"] != payload {
		t.Fatal("file-sized result truncated")
	}
}
