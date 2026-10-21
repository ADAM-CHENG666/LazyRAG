package workbuddyhost

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type mcpSessionStatus struct {
	ConnectorID string `json:"connector_id"`
	PairingHash string `json:"pairing_hash"`
	CheckedAt   int64  `json:"checked_at"`
}

func pairingHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// ReportMCPSession runs only after the external client completes the MCP
// handshake. A successful ping, not a config file or CDP connection, proves
// that WorkBuddy has started and accepted this MCP server.
func ReportMCPSession(ctx context.Context, home, account, instance string, ping func(context.Context) error) {
	if instance == "" || filepath.Base(instance) != instance || strings.ContainsAny(instance, `/\\`) {
		return
	}
	_, pair, err := Load(home, account)
	if err != nil {
		return
	}
	path := filepath.Join(filepath.Dir(ConfigPath(home)), "mcp-sessions", instance+".json")
	defer os.Remove(path)
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()
	for {
		check, cancel := context.WithTimeout(ctx, 2*time.Second)
		err := ping(check)
		cancel()
		if ctx.Err() != nil {
			return
		}
		if err == nil {
			body, _ := json.Marshal(mcpSessionStatus{pair.ConnectorID, pairingHash(pair.Token), time.Now().UnixMilli()})
			_ = writeAtomic(path, body)
		} else {
			_ = os.Remove(path)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func MCPReady(home, account string) bool {
	_, pair, err := Load(home, account)
	if err != nil {
		return false
	}
	paths, _ := filepath.Glob(filepath.Join(filepath.Dir(ConfigPath(home)), "mcp-sessions", "*.json"))
	for _, path := range paths {
		body, err := os.ReadFile(path)
		var status mcpSessionStatus
		if err != nil || json.Unmarshal(body, &status) != nil {
			continue
		}
		age := time.Now().UnixMilli() - status.CheckedAt
		if age >= 0 && age <= 6000 && status.ConnectorID == pair.ConnectorID && status.PairingHash == pairingHash(pair.Token) {
			return true
		}
	}
	return false
}
