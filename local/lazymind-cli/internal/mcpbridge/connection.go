package mcpbridge

import (
	"context"
	"errors"
	"sync"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// A session belongs to one account. Endpoint changes replace it at the next call;
// a failed tool call is never replayed because its side effects may have committed.
type liveConnection struct {
	mu       sync.Mutex
	bridge   *Bridge
	scope    string
	endpoint string
	session  *mcp.ClientSession
}

func (c *liveConnection) close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.session != nil {
		_ = c.session.Close()
		c.session = nil
	}
}
func (c *liveConnection) call(ctx context.Context, params *mcp.CallToolParams) (*mcp.CallToolResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	scope, err := c.bridge.api.AccountScope()
	if err != nil {
		return nil, err
	}
	if scope != c.scope {
		return nil, errors.New("LazyMind account changed; reconnect the Agent")
	}
	endpoint, err := c.bridge.Endpoint(ctx)
	if err != nil {
		return nil, err
	}
	if endpoint != c.endpoint || c.session == nil {
		if c.session != nil {
			_ = c.session.Close()
			c.session = nil
		}
		c.session, _, c.endpoint, err = c.bridge.Connect(ctx)
		if err != nil {
			return nil, err
		}
	}
	result, err := c.session.CallTool(ctx, params)
	if err != nil {
		_ = c.session.Close()
		c.session = nil
	}
	return result, err
}
func accountGuard(current func() (string, error), expected string) mcp.Middleware {
	return func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
			if method == "tools/call" {
				scope, err := current()
				if err != nil {
					return nil, err
				}
				if scope != expected {
					return nil, errors.New("LazyMind account changed; reconnect the Agent")
				}
			}
			return next(ctx, method, request)
		}
	}
}
