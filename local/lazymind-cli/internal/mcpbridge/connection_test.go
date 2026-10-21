package mcpbridge

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"lazymind/agentconnector/internal/credentials"
)

func TestLiveConnectionMovesToCurrentPortAndRejectsAccountSwitch(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	serve := func(label string, calls *atomic.Int32) *httptest.Server {
		s := mcp.NewServer(&mcp.Implementation{Name: "fixture", Version: "1"}, nil)
		for _, name := range requiredTools {
			s.AddTool(&mcp.Tool{Name: name, InputSchema: map[string]any{"type": "object"}}, func(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
				calls.Add(1)
				return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: label}}}, nil
			})
		}
		return httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return s }, nil))
	}
	var firstCalls, secondCalls atomic.Int32
	first := serve("first", &firstCalls)
	defer first.Close()
	second := serve("second", &secondCalls)
	defer second.Close()
	store, _ := credentials.NewStore(t.TempDir(), "")
	root := t.TempDir()
	save := func(server, user string) {
		t.Helper()
		if err := store.SaveDesktop(credentials.Credentials{ServerURL: server, Username: user, AccessToken: "access", RefreshToken: "refresh"}, root); err != nil {
			t.Fatal(err)
		}
	}
	save(first.URL, "alice")
	bridge, err := New(store)
	if err != nil {
		t.Fatal(err)
	}
	session, _, endpoint, err := bridge.Connect(ctx)
	if err != nil {
		t.Fatal(err)
	}
	scope, _ := store.AccountScope()
	connection := &liveConnection{bridge: bridge, scope: scope, session: session, endpoint: endpoint}
	defer connection.close()
	call := func(want string) {
		t.Helper()
		result, err := connection.call(ctx, &mcp.CallToolParams{Name: requiredTools[0], Arguments: map[string]any{}})
		if err != nil {
			t.Fatal(err)
		}
		if result.Content[0].(*mcp.TextContent).Text != want {
			t.Fatal("used stale MCP session")
		}
	}
	call("first")
	save(second.URL, "alice")
	call("second")
	if firstCalls.Load() != 1 || secondCalls.Load() != 1 {
		t.Fatal("unexpected duplicate call")
	}
	save(second.URL, "bob")
	if _, err := connection.call(ctx, &mcp.CallToolParams{Name: requiredTools[0]}); err == nil || !strings.Contains(err.Error(), "account changed") {
		t.Fatalf("account switch allowed: %v", err)
	}
	if secondCalls.Load() != 1 {
		t.Fatal("called tool under another account")
	}
}
