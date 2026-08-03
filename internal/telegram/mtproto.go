package telegram

import (
	"context"
	crand "crypto/rand"
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/gotd/td/session"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/auth"
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

// ResolvePeer converts a Bot API style chat ID string to a tg.InputPeerClass.
// Channel/supergroup IDs use the -100 prefix convention.
func ResolvePeer(ctx context.Context, api *tg.Client, chatIDStr string) (tg.InputPeerClass, error) {
	id, err := strconv.ParseInt(chatIDStr, 10, 64)
	if err != nil {
		return nil, fmt.Errorf("invalid chat ID %q: %w", chatIDStr, err)
	}

	switch {
	case id > 0:
		// User peer
		return &tg.InputPeerUser{UserID: id}, nil

	case id > -1000000000000:
		// Regular group chat
		return &tg.InputPeerChat{ChatID: -id}, nil

	default:
		// Channel or supergroup (Bot API -100 prefix)
		channelID := -(id + 1000000000000)

		// We need the access_hash, so resolve via channels.GetChannels
		res, err := api.ChannelsGetChannels(ctx, []tg.InputChannelClass{
			&tg.InputChannel{ChannelID: channelID},
		})
		if err != nil {
			return nil, fmt.Errorf("failed to resolve channel %d: %w", channelID, err)
		}

		chats := res.GetChats()
		if len(chats) == 0 {
			return nil, fmt.Errorf("channel %d not found", channelID)
		}

		channel, ok := chats[0].(*tg.Channel)
		if !ok {
			return nil, fmt.Errorf("resolved peer is not a channel")
		}

		return &tg.InputPeerChannel{
			ChannelID:  channel.ID,
			AccessHash: channel.AccessHash,
		}, nil
	}
}

// UploadFileMTProto uploads a file using the MTProto 2GB engine and returns its Telegram Message ID.
func UploadFileMTProto(ctx context.Context, localPath, chatIDStr string) (int, error) {
	return UploadFileMTProtoWithCaption(ctx, localPath, chatIDStr, "")
}

// UploadFileMTProtoWithCaption uploads a file with an optional message caption payload.
func UploadFileMTProtoWithCaption(ctx context.Context, localPath, chatIDStr, caption string) (int, error) {
	var msgID int
	err := Run(ctx, func(ctx context.Context) error {
		api := Client.API()
		u := uploader.NewUploader(api)

		pterm.Info.Printf("Resolving peer %s...\n", chatIDStr)
		peer, err := ResolvePeer(ctx, api, chatIDStr)
		if err != nil {
			return fmt.Errorf("peer resolution failed: %w", err)
		}

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

		var randBuf [8]byte
		_, _ = crand.Read(randBuf[:])
		randomID := int64(binary.LittleEndian.Uint64(randBuf[:]))

		updates, err := api.MessagesSendMedia(ctx, &tg.MessagesSendMediaRequest{
			Peer:     peer,
			Message:  caption,
			RandomID: randomID,
			Media: &tg.InputMediaUploadedDocument{
				File:     upload,
				MimeType: "application/octet-stream",
				Attributes: []tg.DocumentAttributeClass{
					&tg.DocumentAttributeFilename{FileName: filepath.Base(localPath)},
				},
			},
		})
		if err != nil {
			return fmt.Errorf("failed to send document: %w", err)
		}

		switch u := updates.(type) {
		case *tg.Updates:
			for _, update := range u.Updates {
				if newMsg, ok := update.(*tg.UpdateNewMessage); ok {
					msgID = newMsg.Message.GetID()
				} else if newChannelMsg, ok := update.(*tg.UpdateNewChannelMessage); ok {
					msgID = newChannelMsg.Message.GetID()
				}
			}
		case *tg.UpdatesCombined:
			for _, update := range u.Updates {
				if newMsg, ok := update.(*tg.UpdateNewMessage); ok {
					msgID = newMsg.Message.GetID()
				} else if newChannelMsg, ok := update.(*tg.UpdateNewChannelMessage); ok {
					msgID = newChannelMsg.Message.GetID()
				}
			}
		}

		pterm.Success.Println("File sent to Telegram successfully!")
		return nil
	})
	return msgID, err
}
