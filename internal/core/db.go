package core

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"

	// Import the modernc SQLite driver so database/sql can use it seamlessly
	_ "modernc.org/sqlite"
)

var DB *sql.DB

func InitDB() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}

	tupDir := filepath.Join(home, ".tup")
	if err := os.MkdirAll(tupDir, 0700); err != nil {
		return err
	}

	dbPath := filepath.Join(tupDir, "vfs.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return err
	}

	// Create tables if they don't exist
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
		return fmt.Errorf("failed to initialize schema: %w", err)
	}

	DB = db
	return nil
}

// GetChatID resolves a drive alias to its Telegram chat ID.
func GetChatID(alias string) (string, error) {
	var chatID string
	err := DB.QueryRow("SELECT chat_id FROM drive_aliases WHERE alias = ?", alias).Scan(&chatID)
	if err != nil {
		if err == sql.ErrNoRows {
			return "", fmt.Errorf("alias '%s' not found. Use 'tup drive add %s <chat_id>' first", alias, alias)
		}
		return "", err
	}
	return chatID, nil
}

