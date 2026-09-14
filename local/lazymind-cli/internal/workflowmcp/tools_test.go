package workflowmcp

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestWorkflowListPublishesObjectInputSchema(t *testing.T) {
	ctx := context.Background()
	clientTransport, serverTransport := mcp.NewInMemoryTransports()
	server := mcp.NewServer(&mcp.Implementation{Name: "schema-test", Version: "1"}, nil)
	Register(server, &Client{})
	serverSession, err := server.Connect(ctx, serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer serverSession.Close()
	client := mcp.NewClient(&mcp.Implementation{Name: "schema-test-client", Version: "1"}, nil)
	clientSession, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer clientSession.Close()
	listed, err := clientSession.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, tool := range listed.Tools {
		if tool.Name != "workflow.list" {
			continue
		}
		schema, ok := tool.InputSchema.(map[string]any)
		if !ok {
			t.Fatalf("workflow.list input schema=%#v", tool.InputSchema)
		}
		properties, ok := schema["properties"].(map[string]any)
		if schema["type"] != "object" || !ok || len(properties) != 0 {
			t.Fatalf("workflow.list input schema=%#v", schema)
		}
		return
	}
	t.Fatal("workflow.list tool is missing")
}

func TestWorkflowToolCopyPinsGetOnce(t *testing.T) {
	ctx := context.Background()
	clientTransport, serverTransport := mcp.NewInMemoryTransports()
	server := mcp.NewServer(&mcp.Implementation{Name: "schema-test", Version: "1"}, nil)
	Register(server, &Client{})
	serverSession, err := server.Connect(ctx, serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer serverSession.Close()
	client := mcp.NewClient(&mcp.Implementation{Name: "schema-test-client", Version: "1"}, nil)
	clientSession, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer clientSession.Close()
	listed, err := clientSession.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, tool := range listed.Tools {
		got[tool.Name] = tool.Description
	}
	if !containsAll(got["workflow.start"], "workflow.get once", "workflow_id", "revision_id") {
		t.Fatalf("workflow.start description=%q", got["workflow.start"])
	}
	if !containsAll(got["workflow.get"], "Call once after workflow.start", "step_contract.legacy_tools", "scripts/tools.py") {
		t.Fatalf("workflow.get description=%q", got["workflow.get"])
	}
	if !containsAll(got["workflow.step.begin"], "step_contract.legacy_tools", "package files from workflow.get") {
		t.Fatalf("workflow.step.begin description=%q", got["workflow.step.begin"])
	}
	if containsAny(got["workflow.step.begin"], "not MCP", "not a new", "not new") {
		t.Fatalf("workflow.step.begin used a negation: %q", got["workflow.step.begin"])
	}
	if !containsAll(got["workflow.step.submit"], "text, json, image, file, or file_list", "local_path", "value") {
		t.Fatalf("workflow.step.submit description=%q", got["workflow.step.submit"])
	}
}

func containsAll(value string, parts ...string) bool {
	for _, part := range parts {
		if !strings.Contains(value, part) {
			return false
		}
	}
	return true
}

func containsAny(value string, parts ...string) bool {
	for _, part := range parts {
		if strings.Contains(value, part) {
			return true
		}
	}
	return false
}

func TestReadOnlyClassificationCoversEveryWorkflowTool(t *testing.T) {
	readOnly := map[string]bool{
		"workflow.list": true, "workflow.get": true, "workflow.input.get": true,
		"workflow.state": true, "workflow.session.list": true,
		"workflow.artifact.list": true, "workflow.artifact.get": true,
	}
	if len(ToolNames) != 15 {
		t.Fatalf("tool count=%d, want 15", len(ToolNames))
	}
	for _, name := range ToolNames {
		if IsReadOnlyTool(name) != readOnly[name] {
			t.Fatalf("read-only classification for %s is %v", name, IsReadOnlyTool(name))
		}
	}
}

func TestStartResultJSONIncludesPinnedRevision(t *testing.T) {
	body, err := json.Marshal(StartResult{SessionID: "mcp-1", WorkflowID: "test-workflow", RevisionID: "rev-3"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), `"workflow_id":"test-workflow"`) || !strings.Contains(string(body), `"revision_id":"rev-3"`) {
		t.Fatalf("start result=%s", body)
	}
}

func TestGeneratedIDsFitWorkflowPersistence(t *testing.T) {
	for _, prefix := range []string{"mcp-start-", "mcp-step-", "mcp-session-"} {
		id, err := newID(prefix)
		if err != nil {
			t.Fatal(err)
		}
		if len(id) > 36 {
			t.Fatalf("generated ID %q has %d characters", id, len(id))
		}
	}
}

func TestEncodeOutputsKeepsSlotTypeAndAllowsAbsoluteFiles(t *testing.T) {
	workspace := t.TempDir()
	old, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(workspace); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(old) })
	inside := filepath.Join(workspace, "result.txt")
	if err := os.WriteFile(inside, []byte("result"), 0o600); err != nil {
		t.Fatal(err)
	}
	values, err := encodeOutputs([]Output{{Slot: "result", ContentType: "file", LocalPath: "result.txt"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(values) != 1 || values[0]["content_type"] != "file" {
		t.Fatalf("workspace file outputs=%#v", values)
	}
	payload, _ := values[0]["value"].(map[string]any)
	if payload["storage"] != "inline_base64" || payload["mime_type"] == nil {
		t.Fatalf("workspace file payload=%#v", payload)
	}

	outsideDir := t.TempDir()
	outside := filepath.Join(outsideDir, "attachment.txt")
	if err := os.WriteFile(outside, []byte("tmp"), 0o600); err != nil {
		t.Fatal(err)
	}
	values, err = encodeOutputs([]Output{{Slot: "text_attachment", LocalPath: outside}})
	if err != nil {
		t.Fatal(err)
	}
	if values[0]["content_type"] != "file" {
		t.Fatalf("absolute file content_type=%v", values[0]["content_type"])
	}

	parent := filepath.Join(filepath.Dir(workspace), "outside.txt")
	if err := os.WriteFile(parent, []byte("leak"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Remove(parent) })
	if _, err := encodeOutputs([]Output{{Slot: "leak", LocalPath: filepath.Join("..", "outside.txt")}}); err == nil {
		t.Fatal("relative path escaped the workspace")
	}

	values, err = encodeOutputs([]Output{{Slot: "image_attachment", ContentType: "image", Value: "https://placehold.co/640x360.png"}})
	if err != nil {
		t.Fatal(err)
	}
	if values[0]["content_type"] != "image" || values[0]["value"] != "https://placehold.co/640x360.png" {
		t.Fatalf("image value outputs=%#v", values)
	}
}

func TestEncodeOutputsAssignsSequenceWithinEachSlot(t *testing.T) {
	values, err := encodeOutputs([]Output{
		{Slot: "items", Value: "first"},
		{Slot: "items", Value: "second"},
		{Slot: "summary", Value: "only"},
		{Slot: "items", Seq: 5, Value: "explicit"},
		{Slot: "items", Value: "after explicit"},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := []int{1, 2, 1, 5, 6}
	for index, value := range values {
		if value["seq"] != want[index] {
			t.Fatalf("output %d seq=%v, want %d", index, value["seq"], want[index])
		}
	}
}
