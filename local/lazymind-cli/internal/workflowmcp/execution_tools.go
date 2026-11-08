package workflowmcp

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type ExecutionToolsInput struct {
	SessionID       string `json:"session_id" jsonschema:"required,Workflow session identifier"`
	ExecutionID     string `json:"execution_id" jsonschema:"required,Current external execution identifier"`
	ExecutionHandle string `json:"execution_handle" jsonschema:"required,Unchanged execution handle held by the root controller; never forward to a worker"`
}

type ExecutionToolCallInput struct {
	SessionID       string         `json:"session_id" jsonschema:"required,Workflow session identifier"`
	ExecutionID     string         `json:"execution_id" jsonschema:"required,Current external execution identifier"`
	ExecutionHandle string         `json:"execution_handle" jsonschema:"required,Unchanged execution handle held by the root controller; never forward to a worker"`
	ToolName        string         `json:"tool_name" jsonschema:"required,Exact name returned by workflow.tool.list"`
	Source          string         `json:"source,omitempty" jsonschema:"package or global source from workflow.tool.list; required for opaque global tool IDs"`
	Arguments       map[string]any `json:"arguments" jsonschema:"required,Arguments matching the advertised input_schema; use resource references instead of Core-local paths"`
	CommandID       string         `json:"command_id" jsonschema:"required,Stable unique operation ID; reuse unchanged with identical arguments after an uncertain result"`
}

func registerExecutionTools(server *mcp.Server, client *Client) {
	registerTool(server, &mcp.Tool{Name: "workflow.tool.list", Title: "List tools for a Workflow execution",
		Description: "Discover registered package adapters and user-authorized global tools, including optional image/video models. Source warnings identify unavailable catalog sections; other returned tools remain usable. Steps declare capabilities and output requirements, not fixed tool names. The root may execute itself or proxy optional workers; it retains the handle. Choose host capabilities or an available MCP tool that satisfies the shared contract.", Annotations: annotations(true)},
		func(ctx context.Context, _ *mcp.CallToolRequest, input ExecutionToolsInput) (*mcp.CallToolResult, map[string]any, error) {
			value, err := client.ListExecutionTools(ctx, input)
			return nil, value, err
		})
	registerTool(server, &mcp.Tool{Name: "workflow.tool.call", Title: "Call a tool for a Workflow execution",
		Description: "The root controller calls a registered and authorized tool for this execution, directly or on behalf of an optional worker. Use the listed name and schema, unchanged execution_handle, and a stable command_id for unchanged retries. This does not publish or complete a step. Do not send credentials or arbitrary host filesystem paths. If unavailable, show the LazyMind configuration guidance and yield; use an equivalent host capability when it satisfies the contract; otherwise report the missing configuration.", Annotations: annotations(false)},
		func(ctx context.Context, _ *mcp.CallToolRequest, input ExecutionToolCallInput) (*mcp.CallToolResult, map[string]any, error) {
			value, err := client.CallExecutionTool(ctx, input)
			return nil, value, err
		})
}

func executionToolPath(session, execution, handle, action string) (string, error) {
	if strings.TrimSpace(session) == "" || strings.TrimSpace(execution) == "" || strings.TrimSpace(handle) == "" {
		return "", errors.New("session_id, execution_id and execution_handle are required")
	}
	return "/workflow-sessions/" + url.PathEscape(session) + "/hosted-attempts/" + url.PathEscape(execution) + "/tools:" + action, nil
}

func (c *Client) ListExecutionTools(ctx context.Context, input ExecutionToolsInput) (map[string]any, error) {
	path, err := executionToolPath(input.SessionID, input.ExecutionID, input.ExecutionHandle, "list")
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = c.api.DoJSON(ctx, http.MethodPost, path, map[string]any{"execution_handle": input.ExecutionHandle}, &result)
	return result, err
}

func (c *Client) CallExecutionTool(ctx context.Context, input ExecutionToolCallInput) (map[string]any, error) {
	path, err := executionToolPath(input.SessionID, input.ExecutionID, input.ExecutionHandle, "call")
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(input.ToolName) == "" || strings.TrimSpace(input.CommandID) == "" || input.Arguments == nil {
		return nil, errors.New("tool_name, arguments and a stable command_id are required")
	}
	if input.Source != "" && input.Source != "package" && input.Source != "global" {
		return nil, errors.New("source must be package or global")
	}
	payload := map[string]any{"execution_handle": input.ExecutionHandle, "tool_name": input.ToolName,
		"arguments": input.Arguments, "command_id": input.CommandID}
	if input.Source != "" {
		payload["source"] = input.Source
	}
	var result map[string]any
	err = c.mutate(ctx, path, payload, &result, map[string]any{"session_id": input.SessionID, "execution_id": input.ExecutionID, "command_id": input.CommandID})
	return result, err
}

func (c *Client) Renew(ctx context.Context, input ExecutionToolsInput) (map[string]any, error) {
	if strings.TrimSpace(input.SessionID) == "" || strings.TrimSpace(input.ExecutionID) == "" || strings.TrimSpace(input.ExecutionHandle) == "" {
		return nil, errors.New("session_id, execution_id and execution_handle are required")
	}
	var result map[string]any
	err := c.mutate(ctx, "/workflow-sessions/"+url.PathEscape(input.SessionID)+"/hosted-attempts/"+url.PathEscape(input.ExecutionID)+":renew",
		map[string]any{"execution_handle": input.ExecutionHandle}, &result, map[string]any{"session_id": input.SessionID, "execution_id": input.ExecutionID})
	return result, err
}
