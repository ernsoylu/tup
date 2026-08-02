package core

import (
	"database/sql"
	"fmt"
	"path"
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

// GetEntryByPath resolves a full remote path (e.g. "/docs/file.txt") to its VFS entry.
func GetEntryByPath(alias, remotePath string) (*VfsEntry, error) {
	trimmed := strings.Trim(remotePath, "/")
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
		var err error
		if parentID == 0 {
			err = DB.QueryRow(`
				SELECT id, alias, COALESCE(parent_id, 0), name, is_dir, size, sha256, message_id 
				FROM vfs_entries 
				WHERE alias = ? AND (parent_id = 0 OR parent_id IS NULL) AND name = ?`,
				alias, part).
				Scan(&entry.ID, &entry.Alias, &entry.ParentID, &entry.Name, &entry.IsDir, &entry.Size, &entry.Sha256, &entry.MessageID)
		} else {
			err = DB.QueryRow(`
				SELECT id, alias, COALESCE(parent_id, 0), name, is_dir, size, sha256, message_id 
				FROM vfs_entries 
				WHERE alias = ? AND parent_id = ? AND name = ?`,
				alias, parentID, part).
				Scan(&entry.ID, &entry.Alias, &entry.ParentID, &entry.Name, &entry.IsDir, &entry.Size, &entry.Sha256, &entry.MessageID)
		}

		if err != nil {
			return nil, fmt.Errorf("path not found: %s", remotePath)
		}

		current = &entry
		parentID = entry.ID
	}

	if current == nil {
		return nil, fmt.Errorf("path not found: %s", remotePath)
	}

	return current, nil
}

// GetEntryByID loads a single entry by primary key.
func GetEntryByID(id int) (*VfsEntry, error) {
	var entry VfsEntry
	err := DB.QueryRow(`
		SELECT id, alias, COALESCE(parent_id, 0), name, is_dir, size, sha256, message_id
		FROM vfs_entries WHERE id = ?`, id).
		Scan(&entry.ID, &entry.Alias, &entry.ParentID, &entry.Name, &entry.IsDir, &entry.Size, &entry.Sha256, &entry.MessageID)
	if err != nil {
		return nil, err
	}
	return &entry, nil
}

// ListDirectory returns all children for a given parent ID.
func ListDirectory(alias string, parentID int) ([]VfsEntry, error) {
	var rows *sql.Rows
	var err error
	if parentID == 0 {
		rows, err = DB.Query(`
			SELECT id, alias, COALESCE(parent_id, 0), name, is_dir, size, sha256, message_id 
			FROM vfs_entries 
			WHERE alias = ? AND (parent_id = 0 OR parent_id IS NULL)
			ORDER BY is_dir DESC, name ASC`, alias)
	} else {
		rows, err = DB.Query(`
			SELECT id, alias, COALESCE(parent_id, 0), name, is_dir, size, sha256, message_id 
			FROM vfs_entries 
			WHERE alias = ? AND parent_id = ?
			ORDER BY is_dir DESC, name ASC`, alias, parentID)
	}
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

// InsertEntry adds or updates a file/folder in the VFS.
func InsertEntry(e VfsEntry) error {
	_, err := DB.Exec(`
		INSERT OR REPLACE INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id) 
		VALUES (?, ?, ?, ?, ?, ?, ?)`,
		e.Alias, e.ParentID, e.Name, e.IsDir, e.Size, e.Sha256, e.MessageID)
	return err
}

// UpdateEntryParentAndName reparents and/or renames an entry (same drive).
func UpdateEntryParentAndName(id, parentID int, name string) error {
	_, err := DB.Exec(`UPDATE vfs_entries SET parent_id = ?, name = ? WHERE id = ?`, parentID, name, id)
	return err
}

// DeleteEntry deletes a single VFS row (not recursive).
func DeleteEntry(id int) error {
	_, err := DB.Exec("DELETE FROM vfs_entries WHERE id = ?", id)
	return err
}

// DeleteEntryRecursive removes an entry and all descendants (children first).
func DeleteEntryRecursive(id int) error {
	if id == 0 {
		return fmt.Errorf("cannot delete root")
	}
	entry, err := GetEntryByID(id)
	if err != nil {
		return err
	}
	if entry.IsDir {
		children, err := ListDirectory(entry.Alias, entry.ID)
		if err != nil {
			return err
		}
		for _, child := range children {
			if err := DeleteEntryRecursive(child.ID); err != nil {
				return err
			}
		}
	}
	return DeleteEntry(id)
}

// CollectMessageIDs returns Telegram message IDs for files under entry id (recursive).
func CollectMessageIDs(id int) ([]int, error) {
	if id == 0 {
		return nil, nil
	}
	entry, err := GetEntryByID(id)
	if err != nil {
		return nil, err
	}
	var ids []int
	if !entry.IsDir {
		if entry.MessageID != 0 {
			ids = append(ids, entry.MessageID)
		}
		return ids, nil
	}
	children, err := ListDirectory(entry.Alias, entry.ID)
	if err != nil {
		return nil, err
	}
	for _, child := range children {
		sub, err := CollectMessageIDs(child.ID)
		if err != nil {
			return nil, err
		}
		ids = append(ids, sub...)
	}
	return ids, nil
}

// Walk visits every entry under parentID depth-first. fullPath is absolute ("/a/b").
func Walk(alias string, parentID int, basePath string, fn func(e VfsEntry, fullPath string) error) error {
	children, err := ListDirectory(alias, parentID)
	if err != nil {
		return err
	}
	for _, child := range children {
		full := path.Join(basePath, child.Name)
		if full == "" || full[0] != '/' {
			full = "/" + strings.TrimPrefix(full, "/")
		}
		if err := fn(child, full); err != nil {
			return err
		}
		if child.IsDir {
			if err := Walk(alias, child.ID, full, fn); err != nil {
				return err
			}
		}
	}
	return nil
}

// Du sums file sizes under the given entry (directories contribute children's sizes).
func Du(id int) (int64, error) {
	if id == 0 {
		// root: sum all top-level
		// caller should pass alias root via listing
		return 0, fmt.Errorf("use DuAlias for root")
	}
	entry, err := GetEntryByID(id)
	if err != nil {
		return 0, err
	}
	if !entry.IsDir {
		return entry.Size, nil
	}
	children, err := ListDirectory(entry.Alias, entry.ID)
	if err != nil {
		return 0, err
	}
	var total int64
	for _, child := range children {
		n, err := Du(child.ID)
		if err != nil {
			return 0, err
		}
		total += n
	}
	return total, nil
}

// DuAlias sums all file sizes on a drive under parentID (0 = entire drive).
func DuAlias(alias string, parentID int) (int64, error) {
	children, err := ListDirectory(alias, parentID)
	if err != nil {
		return 0, err
	}
	var total int64
	for _, child := range children {
		if child.IsDir {
			n, err := Du(child.ID)
			if err != nil {
				return 0, err
			}
			total += n
		} else {
			total += child.Size
		}
	}
	return total, nil
}

// ResolveParentID returns the parent directory ID for an absolute remote path's parent.
// For "/a/b/c.txt" returns the ID of "/a/b". Root parent is 0.
func ResolveParentID(alias, fullPath string) (int, error) {
	dir := path.Dir(strings.TrimSuffix(fullPath, "/"))
	if dir == "." || dir == "/" {
		return 0, nil
	}
	entry, err := GetEntryByPath(alias, dir)
	if err != nil {
		return 0, err
	}
	if !entry.IsDir {
		return 0, fmt.Errorf("parent is not a directory: %s", dir)
	}
	return entry.ID, nil
}

// EntryExists reports whether a path exists.
func EntryExists(alias, remotePath string) bool {
	e, err := GetEntryByPath(alias, remotePath)
	return err == nil && e != nil
}
