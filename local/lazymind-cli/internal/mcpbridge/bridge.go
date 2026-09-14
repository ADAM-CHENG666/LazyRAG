package mcpbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"sort"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"lazymind/agentconnector/internal/coreapi"
	"lazymind/agentconnector/internal/credentials"
	"lazymind/agentconnector/internal/workflowmcp"
)

const workflowInstructions = "When the user chooses a LazyMind Workflow, call workflow.start, then workflow.get once with the returned workflow_id and revision_id. Then workflow.step.begin for a ready step. Run the returned step_contract and submit every required_output with workflow.step.submit using the slot content_type; files use local_path. If the contract lists legacy_tools, run those functions from the package files already returned by workflow.get; if a function cannot run, still produce the required_outputs and submit. Slot writes named in the contract go through submit outputs. An execution_handle is required and must be submitted unchanged. If executor_host is lazymind, only observe workflow.state. After submit, follow control.continuation: awaiting_user ends this turn; draining finishes already granted work. Human review happens after submit. Resolve inputs with workflow.input.get or workflow.artifact.get. Recover a panel execution_id with workflow.step.claim, a dropped session with workflow.session.list, and an interrupted step with workflow.step.resume. Stop the session with workflow.session.stop. Report complete only when workflow.state says completed, then give users URLs from workflow.artifact.list."

var requiredTools = []string{
	"cloud_document.get",
	"cloud_document.list",
	"cloud_document.search",
	"knowledge.document.get",
	"knowledge.document.list",
	"knowledge.list",
	"knowledge.search",
	"skill.get",
	"skill.list",
}

type Bridge struct {
	home                string
	api                 *coreapi.Client
	connectorInstanceID string
	sourceProvider      string
}

type ProbeResult struct {
	Endpoint string   `json:"endpoint"`
	Tools    []string `json:"tools"`
}

func New(store *credentials.Store) (*Bridge, error) {
	api, err := coreapi.New(store)
	if err != nil {
		return nil, err
	}
	instanceID, err := newInvocationID("connector-")
	if err != nil {
		return nil, err
	}
	return &Bridge{
		api: api, connectorInstanceID: instanceID, home: store.Directory(),
		sourceProvider: strings.ToLower(strings.TrimSpace(os.Getenv("LAZYMIND_AGENT_PROVIDER"))),
	}, nil
}

func (b *Bridge) Endpoint(ctx context.Context) (string, error) {
	return b.api.MCPURL(ctx)
}

func (b *Bridge) Connect(ctx context.Context) (*mcp.ClientSession, []*mcp.Tool, string, error) {
	endpoint, err := b.Endpoint(ctx)
	if err != nil {
		return nil, nil, "", wrapMCPStartupError(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "lazymind-agent-bridge", Version: "v1"}, &mcp.ClientOptions{
		Logger: discardLogger(),
	})
	session, err := client.Connect(ctx, &mcp.StreamableClientTransport{
		Endpoint:             endpoint,
		HTTPClient:           b.api.HTTPClient(),
		MaxRetries:           -1,
		DisableStandaloneSSE: true,
	}, nil)
	if err != nil {
		return nil, nil, endpoint, wrapMCPStartupError(fmt.Errorf("connect LazyMind MCP at %s: %w", endpoint, err))
	}
	tools, err := listAllTools(ctx, session)
	if err != nil {
		_ = session.Close()
		return nil, nil, endpoint, wrapMCPStartupError(fmt.Errorf("list LazyMind MCP tools: %w", err))
	}
	if missing := missingRequiredTools(tools); len(missing) > 0 {
		_ = session.Close()
		return nil, nil, endpoint, fmt.Errorf("LazyMind MCP is missing required tools: %s", strings.Join(missing, ", "))
	}
	return session, tools, endpoint, nil
}

// wrapMCPStartupError keeps auth failures visible when DSH hosts mcp proxy.
func wrapMCPStartupError(err error) error {
	if err == nil {
		return nil
	}
	if credentials.IsAuthenticationRequired(err) || strings.Contains(err.Error(), "not logged in to LazyMind") {
		return fmt.Errorf("%w", err)
	}
	return err
}

func (b *Bridge) Probe(ctx context.Context) (ProbeResult, error) {
	session, tools, endpoint, err := b.Connect(ctx)
	if err != nil {
		return ProbeResult{}, err
	}
	defer session.Close()
	names := make([]string, 0, len(tools)+len(workflowmcp.ToolNames))
	for _, tool := range tools {
		names = append(names, tool.Name)
	}
	names = append(names, workflowmcp.ToolNames...)
	sort.Strings(names)
	return ProbeResult{Endpoint: endpoint, Tools: names}, nil
}

func (b *Bridge) RunStdio(ctx context.Context) error {
	upstream, tools, _, err := b.Connect(ctx)
	if err != nil {
		return err
	}
	defer upstream.Close()

	server := mcp.NewServer(&mcp.Implementation{Name: "lazymind", Version: "v2"}, &mcp.ServerOptions{
		Logger: discardLogger(), Instructions: workflowInstructions,
	})
	readOnlyTools := make(map[string]bool, len(tools)+len(workflowmcp.ToolNames))
	for _, publishedTool := range tools {
		tool := publishedTool
		readOnlyTools[tool.Name] = tool.Annotations != nil && tool.Annotations.ReadOnlyHint
		server.AddTool(tool, func(callCtx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			if request == nil || request.Params == nil {
				return nil, errors.New("missing tool call parameters")
			}
			var arguments any = map[string]any{}
			if len(request.Params.Arguments) > 0 {
				if err := json.Unmarshal(request.Params.Arguments, &arguments); err != nil {
					return nil, fmt.Errorf("decode tool arguments: %w", err)
				}
			}
			return upstream.CallTool(callCtx, &mcp.CallToolParams{
				Meta:           request.Params.Meta,
				Name:           request.Params.Name,
				Arguments:      arguments,
				InputResponses: request.Params.InputResponses,
				RequestState:   request.Params.RequestState,
			})
		})
	}
	workflowClient, err := workflowmcp.NewClient(b.api, workflowmcp.StartOrigin{
		ConversationID: os.Getenv("LAZYMIND_CONVERSATION_ID"),
		ExternalRef:    os.Getenv("LAZYMIND_EXTERNAL_REF"),
	})
	if err != nil {
		return err
	}
	workflowClient.HostProvider = b.sourceProvider
	workflowClient.RequireHostBinding = b.sourceProvider == "deepseek-harness" && os.Getenv("LAZYMIND_WORKFLOW_HOST_CONTROL") == "1"
	workflowmcp.Register(server, workflowClient)
	for _, name := range workflowmcp.ToolNames {
		readOnlyTools[name] = workflowmcp.IsReadOnlyTool(name)
	}
	server.AddReceivingMiddleware(invocationMiddleware(
		b.api, b.connectorInstanceID, b.sourceProvider, readOnlyTools,
	))
	err = server.Run(ctx, &mcp.StdioTransport{})
	if errors.Is(err, context.Canceled) {
		return nil
	}
	return err
}

func listAllTools(ctx context.Context, session *mcp.ClientSession) ([]*mcp.Tool, error) {
	var tools []*mcp.Tool
	cursor := ""
	for {
		page, err := session.ListTools(ctx, &mcp.ListToolsParams{Cursor: cursor})
		if err != nil {
			return nil, err
		}
		tools = append(tools, page.Tools...)
		if page.NextCursor == "" {
			return tools, nil
		}
		cursor = page.NextCursor
	}
}

func missingRequiredTools(tools []*mcp.Tool) []string {
	present := make(map[string]struct{}, len(tools))
	for _, tool := range tools {
		if tool != nil {
			present[tool.Name] = struct{}{}
		}
	}
	var missing []string
	for _, name := range requiredTools {
		if _, ok := present[name]; !ok {
			missing = append(missing, name)
		}
	}
	return missing
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}
