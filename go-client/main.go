// Command mail-mcp-go-client is a Go MCP client for mail-mcp-server.
//
// It spawns the Python MCP server over stdio, lists its tools, searches the
// mailbox, and scores one message's priority, printing each step as JSON.
// It demonstrates the Go side of the MCP stack: the same server speaks MCP
// to a Python client (src/agent.py) and to this Go client.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func textOf(content []mcp.Content) []string {
	var out []string
	for _, c := range content {
		if tc, ok := c.(*mcp.TextContent); ok {
			out = append(out, tc.Text)
		}
	}
	return out
}

func printJSON(label string, v any) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		log.Fatalf("%s: marshal: %v", label, err)
	}
	fmt.Printf("== %s ==\n%s\n", label, b)
}

func main() {
	root := flag.String("root", "..", "project root (mail-mcp-server dir)")
	python := flag.String("python", "", "python binary for the server (default: <root>/.venv/bin/python)")
	query := flag.String("query", "cron", "mailbox search query")
	flag.Parse()

	// Resolve to absolute paths: the child process runs with Dir=root, and a
	// relative executable path would be resolved against that directory.
	absRoot, err := filepath.Abs(*root)
	if err != nil {
		log.Fatalf("resolve root: %v", err)
	}
	py := *python
	if py == "" {
		py = filepath.Join(absRoot, ".venv", "bin", "python")
	}
	if _, err := os.Stat(py); err != nil {
		log.Fatalf("python not found at %s (run ./run.sh setup from %s first): %v", py, *root, err)
	}

	ctx := context.Background()
	client := mcp.NewClient(&mcp.Implementation{Name: "mail-mcp-go-client", Version: "v0.1.0"}, nil)
	cmd := exec.Command(py, "-m", "src.server")
	cmd.Dir = absRoot
	transport := &mcp.CommandTransport{Command: cmd}
	session, err := client.Connect(ctx, transport, nil)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	defer session.Close()

	tools, err := session.ListTools(ctx, &mcp.ListToolsParams{})
	if err != nil {
		log.Fatalf("list tools: %v", err)
	}
	names := make([]string, 0, len(tools.Tools))
	for _, t := range tools.Tools {
		names = append(names, t.Name)
	}
	printJSON("tools", names)

	search, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name:      "search_mail",
		Arguments: map[string]any{"query": *query, "limit": 3},
	})
	if err != nil {
		log.Fatalf("search_mail: %v", err)
	}
	printJSON("search_mail", textOf(search.Content))

	if len(search.Content) == 0 {
		fmt.Println("no messages found")
		return
	}
	var first map[string]any
	if err := json.Unmarshal([]byte(textOf(search.Content)[0]), &first); err != nil {
		log.Fatalf("parse first result: %v", err)
	}
	priority, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name:      "suggest_priority",
		Arguments: map[string]any{"message_id": first["id"]},
	})
	if err != nil {
		log.Fatalf("suggest_priority: %v", err)
	}
	printJSON("suggest_priority", textOf(priority.Content))
}
