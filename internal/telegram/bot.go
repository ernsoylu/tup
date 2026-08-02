package telegram

import (
	"context"
	"fmt"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"github.com/ernsoylu/tup/internal/core"
)

var Bot *tgbotapi.BotAPI

func InitBot() error {
	token := core.AppConfig.TelegramBotToken
	if token == "" {
		return fmt.Errorf("TELEGRAM_BOT_TOKEN is not set in config")
	}

	bot, err := tgbotapi.NewBotAPI(token)
	if err != nil {
		return fmt.Errorf("failed to initialize telegram bot: %w", err)
	}

	Bot = bot
	return nil
}

// EditMessageCaption edits the caption of a file already uploaded to a chat.
func EditMessageCaption(ctx context.Context, chatID int64, messageID int, newCaption string) error {
	msg := tgbotapi.NewEditMessageCaption(chatID, messageID, newCaption)
	_, err := Bot.Send(msg)
	return err
}

// DeleteMessage deletes a message in a chat.
func DeleteMessage(ctx context.Context, chatID int64, messageID int) error {
	msg := tgbotapi.NewDeleteMessage(chatID, messageID)
	_, err := Bot.Send(msg)
	return err
}
