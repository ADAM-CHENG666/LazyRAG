package workflowmcp

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"reflect"

	"github.com/google/jsonschema-go/jsonschema"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"lazymind/agentconnector/internal/coreapi"
)

// Keep the server's public diagnostics intact, including fields added by Core
// after this connector was released. Never serialize request credentials.
func diagnostic(err error) map[string]any {
	result := map[string]any{"code": "ADAPTER_ERROR", "message": err.Error(), "retryable": false}
	var problem *coreapi.Error
	if errors.As(err, &problem) {
		for key, value := range problem.Fields {
			result[key] = value
		}
		if problem.Code != "" {
			result["code"] = problem.Code
		}
		result["message"], result["retryable"] = problem.Message, problem.Retryable
		if err != problem {
			result["context"] = err.Error()
		}
		if problem.StatusCode != 0 {
			result["http_status"] = problem.StatusCode
		}
	}
	return result
}

// registerTool is the sole MCP result adapter. Ordinary SDK errors otherwise
// discard structured diagnostics and any domain receipt returned with them.
func registerTool[In, Out any](server *mcp.Server, tool *mcp.Tool, handler mcp.ToolHandlerFor[In, Out]) {
	if tool.OutputSchema == nil && reflect.TypeFor[Out]() != reflect.TypeFor[any]() {
		schema, err := jsonschema.For[Out](nil)
		if err != nil {
			panic(err)
		}
		tool.OutputSchema = map[string]any{"type": "object", "anyOf": []any{schema, map[string]any{
			"type": "object", "required": []string{"error"},
			"properties": map[string]any{"error": map[string]any{"type": "object"}, "result": map[string]any{}},
		}}}
	}
	mcp.AddTool(server, tool, func(ctx context.Context, request *mcp.CallToolRequest, input In) (*mcp.CallToolResult, any, error) {
		result, value, err := handler(ctx, request, input)
		if err == nil {
			return result, value, nil
		}
		payload := map[string]any{"error": diagnostic(err)}
		var problem *coreapi.Error
		if errors.As(err, &problem) && len(problem.Result) > 0 && string(problem.Result) != "null" {
			payload["result"] = problem.Result
		}
		encoded, _ := json.Marshal(payload)
		return &mcp.CallToolResult{IsError: true, StructuredContent: payload,
			Content: []mcp.Content{&mcp.TextContent{Text: string(encoded)}}}, nil, nil
	})
}

// A lost write response is not evidence that the write failed. Include stable
// recovery identifiers even when the connector generated them for the caller.
func (c *Client) mutate(ctx context.Context, path string, input, output any, identifiers map[string]any) error {
	err := c.api.DoJSON(ctx, http.MethodPost, path, input, output)
	if err == nil {
		return nil
	}
	var problem *coreapi.Error
	var notSent *coreapi.RequestNotSentError
	if errors.As(err, &notSent) {
		problem = &coreapi.Error{Code: "REQUEST_NOT_SENT", Message: notSent.Error(), Fields: map[string]any{
			"outcome":  "not_sent",
			"recovery": "Correct the local request or connection error before retrying; no Workflow write was sent.",
		}}
	} else if !errors.As(err, &problem) {
		problem = &coreapi.Error{Code: "REQUEST_OUTCOME_UNKNOWN", Message: "No confirmed response was received from LazyMind; the operation may have been applied"}
	} else {
		clone := *problem
		problem = &clone
	}
	fields := map[string]any{}
	for key, value := range problem.Fields {
		fields[key] = value
	}
	fields["operation"] = identifiers
	if problem.Code == "REQUEST_OUTCOME_UNKNOWN" || problem.StatusCode >= 500 {
		fields["outcome"] = "unknown"
		if fields["recovery"] == nil || fields["recovery"] == "" {
			fields["recovery"] = "Read workflow.state and recover the same session/execution. For commands, replay the original command_id with unchanged inputs; do not create duplicate work."
		}
	}
	problem.Fields = fields
	return problem
}
