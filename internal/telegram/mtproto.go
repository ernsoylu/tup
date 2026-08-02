package telegram

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/session"
)

var Client *telegram.Client

func InitMTProto() error {
	apiID := core.AppConfig.TelegramAPIID
	apiHash := core.AppConfig.TelegramAPIHash

	if apiID == 0 || apiHash == "" {
		return fmt.Errorf("TELEGRAM_API_ID or TELEGRAM_API_HASH is missing in config")
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}

	sessionFile := filepath.Join(home, ".tup", "tup-gotd.session")

	Client = telegram.NewClient(apiID, apiHash, telegram.Options{
		SessionStorage: &session.FileStorage{
			Path: sessionFile,
		},
	})

	return nil
}

// Run executes the given callback inside the active MTProto client connection.
func Run(ctx context.Context, f func(ctx context.Context) error) error {
	return Client.Run(ctx, func(ctx context.Context) error {
		// Verify if the client is actually authorized
		status, err := Client.Auth().Status(ctx)
		if err != nil {
			return fmt.Errorf("failed to check auth status: %w", err)
		}
		if !status.Authorized {
			return fmt.Errorf("MTProto client is not authorized. Please run 'tup login'")
		}
		return f(ctx)
	})
}
