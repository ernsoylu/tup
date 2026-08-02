package cli

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"os"
	"testing"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/ernsoylu/tup/internal/vfs"
	_ "modernc.org/sqlite"
)

func setupTestDB(t *testing.T) {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("failed to open in-memory db: %v", err)
	}
	schema := `
	CREATE TABLE IF NOT EXISTS drive_aliases (
		alias TEXT PRIMARY KEY,
		chat_id TEXT NOT NULL
	);
	
	CREATE TABLE IF NOT EXISTS vfs_entries (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		alias TEXT NOT NULL,
		parent_id INTEGER,
		name TEXT NOT NULL,
		is_dir BOOLEAN NOT NULL,
		size INTEGER,
		sha256 TEXT,
		message_id INTEGER,
		FOREIGN KEY (alias) REFERENCES drive_aliases(alias),
		UNIQUE(alias, parent_id, name)
	);`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("failed to create schema: %v", err)
	}
	core.DB = db
}

func TestPrintTreeJSON(t *testing.T) {
	setupTestDB(t)

	_, err := core.DB.Exec("INSERT INTO drive_aliases (alias, chat_id) VALUES ('VFS', '-100123456')")
	if err != nil {
		t.Fatalf("failed to insert alias: %v", err)
	}

	// Insert directory /docs (id: 1)
	res, err := core.DB.Exec("INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id) VALUES ('VFS', 0, 'docs', 1, 0, '', 0)")
	if err != nil {
		t.Fatalf("failed to insert dir: %v", err)
	}
	dirID, _ := res.LastInsertId()

	// Insert file /docs/readme.md under docs
	_, err = core.DB.Exec("INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id) VALUES ('VFS', ?, 'readme.md', 0, 1024, 'abc', 42)", dirID)
	if err != nil {
		t.Fatalf("failed to insert file: %v", err)
	}

	// Capture stdout
	oldStdout := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w

	dirEntry := &core.VfsEntry{
		ID:       int(dirID),
		Alias:    "VFS",
		ParentID: 0,
		Name:     "docs",
		IsDir:    true,
	}
	pathInfo := vfs.PathInfo{IsRemote: true, Alias: "VFS", Path: "/docs"}

	err = printTreeJSON(pathInfo, dirEntry)
	_ = w.Close()
	os.Stdout = oldStdout

	if err != nil {
		t.Fatalf("printTreeJSON returned error: %v", err)
	}

	var buf bytes.Buffer
	_, _ = buf.ReadFrom(r)

	var result treeJSON
	if err := json.Unmarshal(buf.Bytes(), &result); err != nil {
		t.Fatalf("failed to parse treeJSON output: %v\nOutput: %s", err, buf.String())
	}

	if result.Alias != "VFS" || result.Path != "VFS:/docs" {
		t.Errorf("unexpected path/alias: %+v", result)
	}

	if result.Summary.Files != 1 || result.Summary.Directories != 0 {
		t.Errorf("unexpected summary: %+v", result.Summary)
	}

	if len(result.Tree.Children) != 1 || result.Tree.Children[0].Name != "readme.md" {
		t.Errorf("unexpected tree children: %+v", result.Tree)
	}

	if result.Tree.Children[0].Size != 1024 || result.Tree.Children[0].MessageID != 42 {
		t.Errorf("unexpected child details: %+v", result.Tree.Children[0])
	}
}

func TestBackupCmdJSON(t *testing.T) {
	setupTestDB(t)

	_, err := core.DB.Exec("INSERT INTO drive_aliases (alias, chat_id) VALUES ('VFS', '-100123456')")
	if err != nil {
		t.Fatalf("failed to insert alias: %v", err)
	}

	_, err = core.DB.Exec("INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id) VALUES ('VFS', 0, 'file.txt', 0, 500, 'hash', 100)")
	if err != nil {
		t.Fatalf("failed to insert file: %v", err)
	}

	oldStdout := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w

	RootCmd.SetArgs([]string{"backup", "VFS", "--json"})
	err = RootCmd.Execute()

	_ = w.Close()
	os.Stdout = oldStdout

	if err != nil {
		t.Fatalf("backupCmd returned error: %v", err)
	}

	var buf bytes.Buffer
	_, _ = buf.ReadFrom(r)

	var entries []VfsBackupEntry
	if err := json.Unmarshal(buf.Bytes(), &entries); err != nil {
		t.Fatalf("failed to parse backup JSON: %v\nOutput: %s", err, buf.String())
	}

	if len(entries) != 1 || entries[0].Name != "file.txt" || entries[0].Size != 500 {
		t.Errorf("unexpected backup entries: %+v", entries)
	}
}
