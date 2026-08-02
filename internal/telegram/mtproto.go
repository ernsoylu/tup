package telegram

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/gotd/td/session"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/auth"
	"github.com/gotd/td/telegram/message"
	"github.com/gotd/td/telegram/uploader"
	"github.com/gotd/td/tg"
	"github.com/pterm/pterm"
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
	if Client == nil {
		if err := InitMTProto(); err != nil {
			return err
		}
	}
	return Client.Run(ctx, func(ctx context.Context) error {
		status, err := Client.Auth().Status(ctx)
		if err != nil {
			return fmt.Errorf("failed to check auth status: %w", err)
		}

		if !status.Authorized {
			pterm.Info.Println("Not authorized, starting user login flow...")
			flow := auth.NewFlow(termAuth{}, auth.SendCodeOptions{})
			if err := Client.Auth().IfNecessary(ctx, flow); err != nil {
				return fmt.Errorf("user login failed: %w", err)
			}
			pterm.Success.Println("User login successful!")
		}

		return f(ctx)
	})
}

// termAuth implements the auth.UserAuthenticator interface using pterm
type termAuth struct{}

func (termAuth) Phone(ctx context.Context) (string, error) {
	phone, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter your phone number (e.g. +1234567890)").Show()
	return phone, nil
}

func (termAuth) Password(ctx context.Context) (string, error) {
	pass, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter your 2FA password").WithMask("*").Show()
	return pass, nil
}

func (termAuth) AcceptTermsOfService(ctx context.Context, tos tg.HelpTermsOfService) error {
	return nil
}

func (termAuth) SignUp(ctx context.Context) (auth.UserInfo, error) {
	return auth.UserInfo{}, fmt.Errorf("sign up not supported via tup CLI")
}

func (termAuth) Code(ctx context.Context, sentCode *tg.AuthSentCode) (string, error) {
	code, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter the code sent to your Telegram").Show()
	return code, nil
}

// UploadFileMTProto uploads a file using the MTProto 2GB engine.
func UploadFileMTProto(ctx context.Context, localPath, chatIDStr string) error {
	return Run(ctx, func(ctx context.Context) error {
		api := Client.API()
		sender := message.NewSender(api)
		u := uploader.NewUploader(api)

		pterm.Info.Printf("Resolving peer %s...\n", chatIDStr)
		// Try resolving the peer
		req := sender.Resolve(chatIDStr)

		pterm.Info.Printf("Uploading %s to Telegram (up to 2GB)...\n", localPath)

		f, err := os.Open(localPath)
		if err != nil {
			return err
		}
		defer func() { _ = f.Close() }()

		upload, err := u.FromReader(ctx, localPath, f)
		if err != nil {
			return fmt.Errorf("upload failed: %w", err)
		}

		pterm.Info.Println("Upload complete, finalizing message...")

		msg, err := req.File(ctx, upload)
		if err != nil {
			return fmt.Errorf("failed to send document: %w", err)
		}

		_ = msg
		pterm.Success.Println("File sent to Telegram successfully!")
		return nil
	})
}
