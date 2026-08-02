package core

import (
	"fmt"
	"strings"
)

type VfsEntry struct {
	ID        int
	Alias     string
	ParentID  int
	Name      string
	IsDir     bool
	Size      int64
	Sha256    string
	MessageID int
}

// GetEntryByPath resolves a full remote path (e.g. "/docs/file.txt") to its VFS entry
func GetEntryByPath(alias, path string) (*VfsEntry, error) {
	trimmed := strings.Trim(path, "/")
	if trimmed == "" {
		return &VfsEntry{ID: 0, Alias: alias, IsDir: true, Name: "/"}, nil
	}

	parts := strings.Split(trimmed, "/")
	parentID := 0

	var current *VfsEntry

	for _, part := range parts {
		if part == "" {
			continue
		}

		var entry VfsEntry
		err := DB.QueryRow(`
			SELECT id, alias, parent_id, name, is_dir, size, sha256, message_id 
			FROM vfs_entries 
			WHERE alias = ? AND parent_id = ? AND name = ?`,
			alias, parentID, part).
			Scan(&entry.ID, &entry.Alias, &entry.ParentID, &entry.Name, &entry.IsDir, &entry.Size, &entry.Sha256, &entry.MessageID)

		if err != nil {
			return nil, fmt.Errorf("path not found: %s", path)
		}

		current = &entry
		parentID = entry.ID
	}

	if current == nil {
		return nil, fmt.Errorf("path not found: %s", path)
	}

	return current, nil
}

// ListDirectory returns all children for a given parent ID
func ListDirectory(alias string, parentID int) ([]VfsEntry, error) {
	rows, err := DB.Query(`
		SELECT id, alias, parent_id, name, is_dir, size, sha256, message_id 
		FROM vfs_entries 
		WHERE alias = ? AND parent_id = ?
		ORDER BY is_dir DESC, name ASC`, alias, parentID)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()

	var entries []VfsEntry
	for rows.Next() {
		var e VfsEntry
		if err := rows.Scan(&e.ID, &e.Alias, &e.ParentID, &e.Name, &e.IsDir, &e.Size, &e.Sha256, &e.MessageID); err != nil {
			return nil, err
		}
		entries = append(entries, e)
	}

	return entries, nil
}

// InsertEntry adds or updates a file/folder in the VFS
func InsertEntry(e VfsEntry) error {
	_, err := DB.Exec(`
		INSERT OR REPLACE INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id) 
		VALUES (?, ?, ?, ?, ?, ?, ?)`,
		e.Alias, e.ParentID, e.Name, e.IsDir, e.Size, e.Sha256, e.MessageID)
	return err
}

// DeleteEntry recursively deletes an entry and its children (if dir)
func DeleteEntry(id int) error {
	// Simple recursive CTE or just application level for SQLite
	// For now, if it's a file, just delete it.
	_, err := DB.Exec("DELETE FROM vfs_entries WHERE id = ?", id)
	return err
}
