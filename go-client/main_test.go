package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// projectRoot resolves the mail-mcp-server directory from this test file.
func projectRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	return filepath.Dir(filepath.Dir(file))
}

func connectTestServer(t *testing.T) *mcp.ClientSession {
	t.Helper()
	root := projectRoot(t)
	py := filepath.Join(root, ".venv", "bin", "python")
	if _, err := os.Stat(py); err != nil {
		t.Skipf("venv python not found at %s (run ./run.sh setup first)", py)
	}
	if _, err := os.Stat(filepath.Join(root, "data", "mailbox.db")); err != nil {
		t.Skipf("mailbox.db not found (run ./run.sh build first)")
	}
	ctx := context.Background()
	client := mcp.NewClient(&mcp.Implementation{Name: "mail-mcp-go-client-test", Version: "v0.1.0"}, nil)
	cmd := exec.Command(py, "-m", "src.server")
	cmd.Dir = root
	session, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { session.Close() })
	return session
}

func TestListTools(t *testing.T) {
	session := connectTestServer(t)
	tools, err := session.ListTools(context.Background(), &mcp.ListToolsParams{})
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	want := map[string]bool{
		"search_mail": true, "get_message": true, "classify_message": true,
		"label_message": true, "suggest_priority": true,
		"summarize_message": true, "inbox_stats": true, "cache_stats": true,
	}
	if len(tools.Tools) != len(want) {
		t.Fatalf("got %d tools, want %d", len(tools.Tools), len(want))
	}
	for _, tool := range tools.Tools {
		if !want[tool.Name] {
			t.Errorf("unexpected tool %q", tool.Name)
		}
	}
}

func TestSearchAndPriority(t *testing.T) {
	session := connectTestServer(t)
	ctx := context.Background()
	search, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name:      "search_mail",
		Arguments: map[string]any{"query": "cron", "limit": 3},
	})
	if err != nil {
		t.Fatalf("search_mail: %v", err)
	}
	if len(search.Content) == 0 {
		t.Fatal("search_mail returned no content")
	}
	var first map[string]any
	texts := textOf(search.Content)
	if err := json.Unmarshal([]byte(texts[0]), &first); err != nil {
		t.Fatalf("parse first result: %v", err)
	}
	id, ok := first["id"].(string)
	if !ok || id == "" {
		t.Fatalf("first result has no id: %v", first)
	}
	priority, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name:      "suggest_priority",
		Arguments: map[string]any{"message_id": id},
	})
	if err != nil {
		t.Fatalf("suggest_priority: %v", err)
	}
	var pr map[string]any
	if err := json.Unmarshal([]byte(textOf(priority.Content)[0]), &pr); err != nil {
		t.Fatalf("parse priority: %v", err)
	}
	band, _ := pr["band"].(string)
	if band != "high" && band != "medium" && band != "low" {
		t.Errorf("unexpected band %q", band)
	}
	if score, _ := pr["score"].(float64); score < 0 || score > 100 {
		t.Errorf("score out of range: %v", score)
	}
}
