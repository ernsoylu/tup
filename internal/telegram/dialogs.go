package telegram

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/gotd/td/tg"
	"github.com/pterm/pterm"
)

// DialogInfo describes a Telegram chat usable as a tup drive.
type DialogInfo struct {
	Title    string
	Type     string // user, bot, group, supergroup, channel
	Username string
	ChatID   string // Bot API style ID (-100 prefix for channels)
}

// ListDialogs fetches the account's dialogs and returns chat metadata.
func ListDialogs(ctx context.Context) ([]DialogInfo, error) {
	var result []DialogInfo

	err := Run(ctx, func(ctx context.Context) error {
		api := Client.API()

		status, err := Client.Auth().Status(ctx)
		if err != nil {
			return err
		}
		if status.User != nil && status.User.Bot {
			return fmt.Errorf("bot accounts cannot list chats directly. Please invite your bot to a group/channel and use the Chat ID, or use user login")
		}

		res, err := api.MessagesGetDialogs(ctx, &tg.MessagesGetDialogsRequest{
			OffsetPeer: &tg.InputPeerEmpty{},
			Limit:      100,
		})
		if err != nil {
			return err
		}

		var chats []tg.ChatClass
		var users []tg.UserClass

		switch d := res.(type) {
		case *tg.MessagesDialogs:
			chats = d.Chats
			users = d.Users
		case *tg.MessagesDialogsSlice:
			chats = d.Chats
			users = d.Users
			if d.Count > len(d.Dialogs) {
				pterm.Warning.Printf("Showing first %d of %d dialogs\n", len(d.Dialogs), d.Count)
			}
		}

		for _, u := range users {
			user, ok := u.(*tg.User)
			if !ok || user.Self {
				continue
			}
			kind := "user"
			if user.Bot {
				kind = "bot"
			}
			result = append(result, DialogInfo{
				Title:    strings.TrimSpace(user.FirstName + " " + user.LastName),
				Type:     kind,
				Username: user.Username,
				ChatID:   strconv.FormatInt(user.ID, 10),
			})
		}

		for _, c := range chats {
			switch chat := c.(type) {
			case *tg.Chat:
				result = append(result, DialogInfo{
					Title:  chat.Title,
					Type:   "group",
					ChatID: strconv.FormatInt(-chat.ID, 10),
				})
			case *tg.Channel:
				kind := "channel"
				if chat.Megagroup {
					kind = "supergroup"
				}
				result = append(result, DialogInfo{
					Title:    chat.Title,
					Type:     kind,
					Username: chat.Username,
					ChatID:   strconv.FormatInt(-(1000000000000 + chat.ID), 10),
				})
			}
		}

		return nil
	})

	return result, err
}
